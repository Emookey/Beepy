from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4
from urllib.parse import quote

from dateutil.parser import isoparse
from sqlalchemy import func, select

from .db import SessionLocal, initialize_database
from .email_graph import GraphClient, EmailGraphConfigurationError, email_indexer_configured
from .models import EmailFolder, EmailMailbox, EmailMessage, SyncState
from .ollama import embed

logger = logging.getLogger(__name__)

MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,subject,from,sender,toRecipients,ccRecipients,bccRecipients,"
    "sentDateTime,receivedDateTime,lastModifiedDateTime,body,bodyPreview,hasAttachments,importance,isRead,webLink"
)
MESSAGE_HEADERS = {
    "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text", odata.maxpagesize=100'
}


def _dt(value):
    if not value:
        return None
    try:
        return isoparse(value)
    except Exception:
        return None


def _email_address(node: dict | None) -> tuple[str, str]:
    address = ((node or {}).get("emailAddress") or {})
    return str(address.get("name") or "").strip(), str(address.get("address") or "").strip().lower()


def _recipients(rows: list | None) -> list[dict]:
    out = []
    for row in rows or []:
        name, address = _email_address(row)
        if address:
            out.append({"name": name, "address": address})
    return out


def _clean_body(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n", text)
    return text.strip()[:60000]


def _document(message: dict, mailbox: EmailMailbox, folder: EmailFolder) -> tuple[str, dict]:
    sender_name, sender_address = _email_address(message.get("from") or message.get("sender"))
    to_rows = _recipients(message.get("toRecipients"))
    cc_rows = _recipients(message.get("ccRecipients"))
    bcc_rows = _recipients(message.get("bccRecipients"))
    subject = str(message.get("subject") or "").strip()
    body = _clean_body(((message.get("body") or {}).get("content")))
    preview = _clean_body(message.get("bodyPreview"))[:1200]
    chunks = [
        f"Mailbox: {mailbox.primary_address}",
        f"Folder: {folder.display_name}",
        f"From: {sender_name} <{sender_address}>" if sender_address else "From:",
        "To: " + ", ".join(x["address"] for x in to_rows),
        "Cc: " + ", ".join(x["address"] for x in cc_rows),
        f"Subject: {subject}",
        f"Sent: {message.get('sentDateTime') or ''}",
        f"Received: {message.get('receivedDateTime') or ''}",
        "",
        body or preview,
    ]
    document = "\n".join(chunks).strip()
    return document, {
        "sender_name": sender_name,
        "sender_address": sender_address,
        "to": to_rows,
        "cc": cc_rows,
        "bcc": bcc_rows,
        "subject": subject,
        "body": body,
        "preview": preview,
    }


def _set_sync_state(status: str, **extra) -> None:
    with SessionLocal() as db:
        row = db.get(SyncState, "email")
        previous = dict(row.value or {}) if row else {}
        value = {**previous, "status": status, "updatedAt": datetime.now(timezone.utc).isoformat(), **extra}
        if row:
            row.value = value
        else:
            db.add(SyncState(key="email", value=value))
        db.commit()


def _upsert_mailboxes(graph: GraphClient, mailbox_filter: str | None = None, max_mailboxes: int | None = None) -> list[EmailMailbox]:
    users: list[dict] = []
    url = "/users"
    params = {
        "$select": "id,displayName,mail,userPrincipalName,accountEnabled,userType",
        "$top": "999",
    }
    for page in graph.iter_pages(url, params=params):
        users.extend(page.get("value") or [])
        if max_mailboxes and len(users) >= max_mailboxes:
            break

    with SessionLocal() as db:
        seen = 0
        for user in users:
            if str(user.get("userType") or "Member").lower() == "guest":
                continue
            address = str(user.get("mail") or user.get("userPrincipalName") or "").strip().lower()
            if not address or "@" not in address:
                continue
            if mailbox_filter and address != mailbox_filter.lower():
                continue
            graph_id = str(user.get("id") or "").strip()
            if not graph_id:
                continue
            row = db.scalar(select(EmailMailbox).where(EmailMailbox.graph_user_id == graph_id))
            if not row:
                row = EmailMailbox(id=str(uuid4()), graph_user_id=graph_id)
                db.add(row)
            row.primary_address = address
            row.user_principal_name = str(user.get("userPrincipalName") or address).lower()
            row.display_name = str(user.get("displayName") or address)
            row.active = True  # include shared mailboxes even when sign-in is disabled
            seen += 1
            if max_mailboxes and seen >= max_mailboxes:
                break
        db.commit()
        ids = [x.id for x in db.scalars(select(EmailMailbox).where(EmailMailbox.active == True).order_by(EmailMailbox.primary_address)).all()]
        rows = [db.get(EmailMailbox, x) for x in ids]
        if mailbox_filter:
            rows = [x for x in rows if x and x.primary_address == mailbox_filter.lower()]
        if max_mailboxes:
            rows = rows[:max_mailboxes]
        return rows


def _walk_folders(graph: GraphClient, mailbox: EmailMailbox) -> list[dict]:
    out: list[dict] = []
    queue: list[tuple[str | None, str]] = [(None, f"/users/{quote(mailbox.graph_user_id)}/mailFolders")]
    while queue:
        parent, url = queue.pop(0)
        for page in graph.iter_pages(url, params={"$top": "100", "includeHiddenFolders": "true", "$select": "id,displayName,parentFolderId,childFolderCount,totalItemCount"}):
            for item in page.get("value") or []:
                folder_id = str(item.get("id") or "")
                if not folder_id:
                    continue
                item["_parent"] = parent
                out.append(item)
                if int(item.get("childFolderCount") or 0) > 0:
                    queue.append((folder_id, f"/users/{quote(mailbox.graph_user_id)}/mailFolders/{quote(folder_id, safe='')}/childFolders"))
    return out


def _upsert_folders(graph: GraphClient, mailbox_id: str, max_folders: int | None = None) -> list[EmailFolder]:
    with SessionLocal() as db:
        mailbox = db.get(EmailMailbox, mailbox_id)
        if not mailbox:
            return []
        remote = _walk_folders(graph, mailbox)
        for item in remote[:max_folders] if max_folders else remote:
            graph_folder_id = str(item.get("id"))
            row = db.scalar(select(EmailFolder).where(
                EmailFolder.mailbox_id == mailbox.id,
                EmailFolder.graph_folder_id == graph_folder_id,
            ))
            if not row:
                row = EmailFolder(id=str(uuid4()), mailbox_id=mailbox.id, graph_folder_id=graph_folder_id)
                db.add(row)
            row.display_name = str(item.get("displayName") or "")[:300]
            row.parent_graph_folder_id = str(item.get("parentFolderId") or item.get("_parent") or "") or None
            row.total_item_count = int(item.get("totalItemCount") or 0)
        db.commit()
        rows = db.scalars(select(EmailFolder).where(EmailFolder.mailbox_id == mailbox.id).order_by(EmailFolder.display_name)).all()
        return list(rows[:max_folders] if max_folders else rows)


def _embed_changed(ids: list[str]) -> int:
    if not ids:
        return 0
    count = 0
    for start in range(0, len(ids), 16):
        batch_ids = ids[start:start + 16]
        with SessionLocal() as db:
            rows = [db.get(EmailMessage, mid) for mid in batch_ids]
            rows = [x for x in rows if x and x.document_text]
            if not rows:
                continue
            texts = [x.document_text[:12000] for x in rows]
            try:
                vectors = embed(texts)
            except Exception as exc:
                logger.warning("Email embedding batch failed: %s", exc)
                continue
            for row, vector in zip(rows, vectors):
                row.embedding = vector
                row.embedding_hash = hashlib.sha256(row.document_text.encode("utf-8", errors="ignore")).hexdigest()
                count += 1
            db.commit()
    return count


def sync_folder(graph: GraphClient, mailbox_id: str, folder_id: str, *, max_pages: int | None = None) -> dict:
    changed_ids: list[str] = []
    processed = deleted = 0
    with SessionLocal() as db:
        mailbox = db.get(EmailMailbox, mailbox_id)
        folder = db.get(EmailFolder, folder_id)
        if not mailbox or not folder:
            return {"processed": 0, "deleted": 0, "embedded": 0}
        next_url = folder.delta_link or (
            f"/users/{quote(mailbox.graph_user_id)}/mailFolders/{quote(folder.graph_folder_id, safe='')}/messages/delta"
            f"?$select={MESSAGE_SELECT}&$top=100"
        )

    page_count = 0
    final_delta = None
    while next_url:
        payload = graph.get(next_url, headers=MESSAGE_HEADERS)
        page_count += 1
        with SessionLocal() as db:
            mailbox = db.get(EmailMailbox, mailbox_id)
            folder = db.get(EmailFolder, folder_id)
            if not mailbox or not folder:
                break
            for item in payload.get("value") or []:
                graph_id = str(item.get("id") or "")
                if not graph_id:
                    continue
                existing = db.scalar(select(EmailMessage).where(
                    EmailMessage.mailbox_id == mailbox.id,
                    EmailMessage.graph_message_id == graph_id,
                ))
                if "@removed" in item:
                    # A moved message can appear as removed from the old folder and
                    # added to the new one. Only delete if our current stored location
                    # is still this folder, so folder-sync ordering cannot erase a move.
                    if existing and existing.folder_id == folder.id:
                        db.delete(existing)
                        deleted += 1
                    continue
                document, meta = _document(item, mailbox, folder)
                if not existing:
                    existing = EmailMessage(
                        id=str(uuid4()), mailbox_id=mailbox.id, folder_id=folder.id,
                        graph_message_id=graph_id,
                    )
                    db.add(existing)
                existing.folder_id = folder.id
                existing.internet_message_id = str(item.get("internetMessageId") or "")[:1000] or None
                existing.conversation_id = str(item.get("conversationId") or "")[:500] or None
                existing.subject = meta["subject"][:2000]
                existing.sender_name = meta["sender_name"][:500]
                existing.sender_address = meta["sender_address"][:500]
                existing.to_recipients = meta["to"]
                existing.cc_recipients = meta["cc"]
                existing.bcc_recipients = meta["bcc"]
                existing.sent_at = _dt(item.get("sentDateTime"))
                existing.received_at = _dt(item.get("receivedDateTime"))
                existing.last_modified_at = _dt(item.get("lastModifiedDateTime"))
                existing.body_text = meta["body"]
                existing.body_preview = meta["preview"]
                existing.has_attachments = bool(item.get("hasAttachments"))
                existing.importance = str(item.get("importance") or "normal")[:30]
                existing.is_read = bool(item.get("isRead"))
                existing.web_link = str(item.get("webLink") or "")[:4000]
                old_hash = existing.embedding_hash
                new_hash = hashlib.sha256(document.encode("utf-8", errors="ignore")).hexdigest()
                existing.document_text = document
                existing.raw = {
                    "internetMessageId": item.get("internetMessageId"),
                    "conversationId": item.get("conversationId"),
                }
                if old_hash != new_hash:
                    existing.embedding = None
                    existing.embedding_hash = None
                    changed_ids.append(existing.id)
                processed += 1
            folder.last_synced_at = datetime.now(timezone.utc)
            mailbox.last_synced_at = datetime.now(timezone.utc)
            db.commit()

        final_delta = payload.get("@odata.deltaLink") or final_delta
        next_url = payload.get("@odata.nextLink")
        if max_pages and page_count >= max_pages:
            logger.warning("Stopped folder %s after max_pages=%s; delta link not advanced.", folder_id, max_pages)
            final_delta = None
            break

    if final_delta:
        with SessionLocal() as db:
            folder = db.get(EmailFolder, folder_id)
            if folder:
                folder.delta_link = final_delta
                folder.last_synced_at = datetime.now(timezone.utc)
                db.commit()

    embedded = _embed_changed(list(dict.fromkeys(changed_ids)))
    return {"processed": processed, "deleted": deleted, "embedded": embedded, "pages": page_count}


def sync_email_all(*, mailbox_filter: str | None = None, max_mailboxes: int | None = None, max_folders: int | None = None, max_pages_per_folder: int | None = None, require_existing_delta: bool = False) -> dict:
    initialize_database()
    if not email_indexer_configured():
        raise EmailGraphConfigurationError("Email indexer credentials are not configured.")
    _set_sync_state("running", mailbox=mailbox_filter)
    totals = {"mailboxes": 0, "folders": 0, "processed": 0, "deleted": 0, "embedded": 0}
    try:
        with GraphClient() as graph:
            mailboxes = _upsert_mailboxes(graph, mailbox_filter, max_mailboxes)
            for mailbox in mailboxes:
                try:
                    folders = _upsert_folders(graph, mailbox.id, max_folders)
                except Exception as exc:
                    # Entra users without Exchange mailboxes are expected in many tenants.
                    logger.warning("Skipping mailbox %s: %s", mailbox.primary_address, exc)
                    continue
                if require_existing_delta:
                    folders = [x for x in folders if x.delta_link]
                totals["mailboxes"] += 1
                totals["folders"] += len(folders)
                logger.info("Email sync mailbox %s: %s folders", mailbox.primary_address, len(folders))
                for folder in folders:
                    try:
                        result = sync_folder(graph, mailbox.id, folder.id, max_pages=max_pages_per_folder)
                        for key in ("processed", "deleted", "embedded"):
                            totals[key] += int(result.get(key) or 0)
                        logger.info("Email sync %s / %s: %s", mailbox.primary_address, folder.display_name, result)
                    except Exception as exc:
                        # Some Entra users do not have Exchange mailboxes. Continue without killing tenant sync.
                        logger.warning("Skipping email folder %s / %s: %s", mailbox.primary_address, folder.display_name, exc)
        with SessionLocal() as db:
            totals["indexedMessages"] = int(db.scalar(select(func.count()).select_from(EmailMessage)) or 0)
        complete_run = (
            not require_existing_delta
            and mailbox_filter is None
            and max_mailboxes is None
            and max_folders is None
            and max_pages_per_folder is None
        )
        if complete_run:
            _set_sync_state("ok", backfillComplete=True, **totals)
        else:
            _set_sync_state("ok", **totals)
        return totals
    except Exception as exc:
        _set_sync_state("error", error=str(exc)[:1000], **totals)
        raise


def main():
    parser = argparse.ArgumentParser(description="Backfill or incrementally synchronize Microsoft 365 email into Beepy.")
    parser.add_argument("--mailbox", help="Only sync one mailbox address")
    parser.add_argument("--max-mailboxes", type=int)
    parser.add_argument("--max-folders", type=int)
    parser.add_argument("--max-pages-per-folder", type=int)
    parser.add_argument("--incremental-only", action="store_true", help="Only folders that already have a saved delta link")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = sync_email_all(
        mailbox_filter=args.mailbox,
        max_mailboxes=args.max_mailboxes,
        max_folders=args.max_folders,
        max_pages_per_folder=args.max_pages_per_folder,
        require_existing_delta=args.incremental_only,
    )
    print(result)


if __name__ == "__main__":
    main()
