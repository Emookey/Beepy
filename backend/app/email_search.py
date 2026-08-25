from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select, text

from .db import SessionLocal
from .models import EmailFolder, EmailMailbox, EmailMessage, EmailSearchAudit
from .ollama import embed


@dataclass
class EmailHit:
    id: str
    internet_message_id: str | None
    subject: str
    sender_name: str
    sender_address: str
    to_recipients: list
    cc_recipients: list
    sent_at: datetime | None
    received_at: datetime | None
    body_text: str
    body_preview: str
    has_attachments: bool
    web_link: str
    mailbox: str
    folder: str
    score: float


def _date_filters(question: str) -> tuple[datetime | None, datetime | None]:
    q = question.lower()
    now = datetime.now(timezone.utc)
    if "this year" in q:
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), None
    if "last year" in q:
        return datetime(now.year - 1, 1, 1, tzinfo=timezone.utc), datetime(now.year, 1, 1, tzinfo=timezone.utc)
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", q)]
    if years:
        year = years[0]
        return datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return None, None


def _clean_search_text(question: str) -> str:
    q = re.sub(r"\b(?:email|emails|emailed|mail|mailbox|inbox|sent items|message|messages|correspondence)\b", " ", question, flags=re.I)
    q = re.sub(r"\b(?:find|search|show|look for|look up|tell me|summarize|summary|what did|when did)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    return q or question.strip()


def _explicit_address_filters(question: str):
    sender = None
    recipient = None
    m = re.search(r"\bfrom\s+([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", question, re.I)
    if m:
        sender = m.group(1).lower()
    m = re.search(r"\bto\s+([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", question, re.I)
    if m:
        recipient = m.group(1).lower()
    return sender, recipient


def _row_to_hit(row, score: float) -> EmailHit:
    msg: EmailMessage = row[0]
    mailbox = row[1]
    folder = row[2]
    return EmailHit(
        id=msg.id,
        internet_message_id=msg.internet_message_id,
        subject=msg.subject or "(no subject)",
        sender_name=msg.sender_name or "",
        sender_address=msg.sender_address or "",
        to_recipients=msg.to_recipients or [],
        cc_recipients=msg.cc_recipients or [],
        sent_at=msg.sent_at,
        received_at=msg.received_at,
        body_text=msg.body_text or "",
        body_preview=msg.body_preview or "",
        has_attachments=bool(msg.has_attachments),
        web_link=msg.web_link or "",
        mailbox=mailbox or "",
        folder=folder or "",
        score=float(score or 0.0),
    )


def search_emails(question: str, limit: int = 25) -> list[EmailHit]:
    limit = min(max(int(limit or 25), 1), 100)
    cleaned = _clean_search_text(question)
    sender, recipient = _explicit_address_filters(question)
    date_from, date_to = _date_filters(question)

    with SessionLocal() as db:
        filters = []
        params = {}
        if sender:
            filters.append("lower(em.sender_address) = :sender")
            params["sender"] = sender
        if recipient:
            filters.append("(em.to_recipients::text ILIKE :recipient OR em.cc_recipients::text ILIKE :recipient)")
            params["recipient"] = f"%{recipient}%"
        if date_from:
            filters.append("COALESCE(em.sent_at, em.received_at) >= :date_from")
            params["date_from"] = date_from
        if date_to:
            filters.append("COALESCE(em.sent_at, em.received_at) < :date_to")
            params["date_to"] = date_to
        where = (" AND " + " AND ".join(filters)) if filters else ""

        candidates: dict[str, tuple[float, object]] = {}
        lexical_q = cleaned[:500]
        if lexical_q:
            params_lex = {**params, "q": lexical_q, "candidate_limit": max(limit * 5, 75)}
            sql = text(f"""
                SELECT em.id,
                       ts_rank_cd(to_tsvector('english', em.document_text), websearch_to_tsquery('english', :q)) AS rank
                FROM email_messages em
                WHERE to_tsvector('english', em.document_text) @@ websearch_to_tsquery('english', :q)
                {where}
                ORDER BY rank DESC, COALESCE(em.sent_at, em.received_at) DESC NULLS LAST
                LIMIT :candidate_limit
            """)
            try:
                for email_id, rank in db.execute(sql, params_lex).all():
                    candidates[str(email_id)] = (float(rank or 0.0) * 4.0, None)
            except Exception:
                pass

        try:
            vector = embed([cleaned])[0]
            params_sem = {**params, "vec": str(vector), "semantic_threshold": 0.40, "candidate_limit": max(limit * 5, 75)}
            sql = text(f"""
                SELECT em.id, 1 - (em.embedding <=> CAST(:vec AS vector)) AS similarity
                FROM email_messages em
                WHERE em.embedding IS NOT NULL
                  AND 1 - (em.embedding <=> CAST(:vec AS vector)) >= :semantic_threshold
                {where}
                ORDER BY em.embedding <=> CAST(:vec AS vector), COALESCE(em.sent_at, em.received_at) DESC NULLS LAST
                LIMIT :candidate_limit
            """)
            for email_id, similarity in db.execute(sql, params_sem).all():
                key = str(email_id)
                current = candidates.get(key, (0.0, None))[0]
                candidates[key] = (current + max(float(similarity or 0.0), 0.0), None)
        except Exception as exc:
            print(f"Email semantic search unavailable: {exc}", flush=True)

        # Never fill an Email search with unrelated recent mail. Zero genuine
        # matches must remain zero; this is intentionally different from a
        # recommendation/search UI that might broaden results.
        if not candidates:
            return []

        ordered_ids = [x[0] for x in sorted(candidates.items(), key=lambda kv: kv[1][0], reverse=True)[: max(limit * 3, 50)]]
        if not ordered_ids:
            return []
        rows = db.execute(
            select(EmailMessage, EmailMailbox.primary_address, EmailFolder.display_name)
            .join(EmailMailbox, EmailMessage.mailbox_id == EmailMailbox.id)
            .join(EmailFolder, EmailMessage.folder_id == EmailFolder.id)
            .where(EmailMessage.id.in_(ordered_ids))
        ).all()
        row_map = {row[0].id: row for row in rows}

        # Deduplicate copies of the same internet message across multiple tenant mailboxes.
        seen: set[str] = set()
        hits: list[EmailHit] = []
        for email_id in ordered_ids:
            row = row_map.get(email_id)
            if not row:
                continue
            msg = row[0]
            canonical = (msg.internet_message_id or f"{msg.sender_address}|{msg.subject}|{msg.sent_at}|{msg.body_preview[:200]}").lower()
            if canonical in seen:
                continue
            seen.add(canonical)
            hits.append(_row_to_hit(row, candidates[email_id][0]))
            if len(hits) >= limit:
                break
        return hits


def _evidence(hits: list[EmailHit]) -> str:
    blocks = []
    for index, hit in enumerate(hits[:20], 1):
        to_text = ", ".join(x.get("address", "") for x in hit.to_recipients if x.get("address"))
        cc_text = ", ".join(x.get("address", "") for x in hit.cc_recipients if x.get("address"))
        when = hit.sent_at or hit.received_at
        body = (hit.body_text or hit.body_preview)[:4500]
        blocks.append(
            f"[Email {index}]\n"
            f"From: {hit.sender_name} <{hit.sender_address}>\n"
            f"To: {to_text}\n"
            f"Cc: {cc_text}\n"
            f"Subject: {hit.subject}\n"
            f"Date: {when.isoformat() if when else ''}\n"
            f"Mailbox copy: {hit.mailbox} / {hit.folder}\n"
            f"Has attachments: {'yes' if hit.has_attachments else 'no'}\n"
            f"Body:\n{body}"
        )
    return "\n\n---\n\n".join(blocks)


def answer_email_question(question: str, hits: list[EmailHit], history: list[dict] | None = None) -> str:
    if not hits:
        return "## No matching email found\n\nI searched the indexed Microsoft 365 email source and did not find a matching message. I did **not** search Autotask tickets or Kal technical knowledge as a fallback."

    # Phase 3E.6B deliberately keeps business evidence out of Kal's technical
    # contract.  Return the locally retrieved records without sending headers,
    # bodies, history, or the user's question to any reasoning service.  The
    # accepted history parameter remains only for API compatibility and is not
    # used here because persisted chat history can contain another mode.
    lines = [
        "## Matching email",
        "",
        "Email synthesis is deferred until a separately approved business-data contract exists. "
        "These are local indexed matches; Kal was not contacted.",
        "",
    ]
    for index, hit in enumerate(hits[:10], 1):
        when = hit.sent_at or hit.received_at
        lines.extend([
            f"**[Email {index}] {hit.subject}**",
            f"From: {hit.sender_name or hit.sender_address} <{hit.sender_address}>" if hit.sender_address else f"From: {hit.sender_name}",
            f"Date: {when.isoformat() if when else 'Unknown'}",
            (hit.body_preview or hit.body_text[:500]).strip(),
            "",
        ])
    return "\n\n".join(lines)


def audit_email_search(user_email: str, question: str, hits: list[EmailHit], elapsed_ms: int) -> None:
    with SessionLocal() as db:
        db.add(EmailSearchAudit(
            id=str(uuid4()),
            user_email=user_email.lower(),
            question=question,
            matched_message_ids=[x.id for x in hits],
            result_count=len(hits),
            elapsed_ms=elapsed_ms,
        ))
        db.commit()
