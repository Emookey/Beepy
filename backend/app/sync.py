from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from dateutil.parser import isoparse
from sqlalchemy import func, select

from .autotask import AutotaskClient
from .config import get_settings
from .db import SessionLocal
from .models import Company, Contact, Resource, SyncState, Ticket, TicketNote
from .ollama import embed

settings = get_settings()
client = AutotaskClient()


def dt(value):
    if not value:
        return None
    try:
        parsed = isoparse(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def display_name(item):
    name = f"{item.get('firstName') or ''} {item.get('lastName') or ''}".strip()
    return (
        name
        or item.get("userName")
        or item.get("emailAddress")
        or str(item.get("id"))
    )


def resolution_from(notes, description=""):
    terms = (
        "resolution",
        "resolved",
        "solution",
        "fixed",
        "completed",
        "work performed",
    )
    candidates = []
    for note in notes:
        text = f"{note.get('title') or ''} {note.get('description') or ''}".strip()
        if text:
            score = sum(5 for term in terms if term in text.lower())
            candidates.append((score, text))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else (description or "")


def build_document(ticket, notes):
    return "\n".join(
        [
            f"Ticket number: {ticket.ticket_number or ''}",
            f"Title: {ticket.title or ''}",
            f"Client: {ticket.company_name or ''}",
            f"Created: {ticket.create_date or ''}",
            f"Assigned to: {ticket.assigned_to or ''}",
            f"Created by: {ticket.created_by or ''}",
            f"Description: {ticket.description or ''}",
            f"Resolution: {ticket.resolution or ''}",
            "Notes:",
            *[
                (
                    f"{note.create_date or ''} {note.creator_name or ''}: "
                    f"{note.title or ''} {note.description or ''}"
                )
                for note in notes
            ],
        ]
    )


def _sync_state(db):
    state = db.get(SyncState, "autotask")
    return state.value if state else {}


def _iso_for_autotask(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_all(force_full: bool = False) -> dict:
    with SessionLocal() as db:
        previous = _sync_state(db)
        ticket_count = db.scalar(select(func.count()).select_from(Ticket)) or 0
        full = force_full or ticket_count == 0 or not previous.get("completedAt")

    sync_started = datetime.now(timezone.utc)
    overlap = timedelta(minutes=settings.sync_overlap_minutes)

    # Refresh reference records so company and resource names remain current.
    companies = client.query_all_by_id("Companies")
    resources = client.query_all_by_id("Resources")
    contacts = client.query_all_by_id("Contacts")

    if full:
        tickets = client.query_all_by_id("Tickets")
        notes = client.query_all_by_id("TicketNotes")
        sync_mode = "full"
    else:
        last_completed = datetime.fromisoformat(previous["completedAt"])
        since = _iso_for_autotask(last_completed - overlap)
        tickets = client.query_since("Tickets", "lastActivityDate", since)
        try:
            notes = client.query_since("TicketNotes", "createDate", since)
        except Exception:
            # Accuracy-first fallback for tenants whose TicketNotes timestamp
            # filtering behaves differently.
            notes = client.query_all_by_id("TicketNotes")
        sync_mode = "incremental"

    company_names = {
        int(item["id"]): item.get("companyName") or str(item["id"])
        for item in companies
    }
    resource_names = {
        int(item["id"]): display_name(item)
        for item in resources
    }

    changed_ticket_ids = set()

    with SessionLocal() as db:
        for item in companies:
            db.merge(
                Company(
                    id=int(item["id"]),
                    name=item.get("companyName") or str(item["id"]),
                    active=bool(item.get("isActive", True)),
                    raw=item,
                )
            )

        for item in resources:
            db.merge(
                Resource(
                    id=int(item["id"]),
                    name=resource_names[int(item["id"])],
                    username=item.get("userName"),
                    active=bool(item.get("isActive", True)),
                    raw=item,
                )
            )

        for item in contacts:
            db.merge(
                Contact(
                    id=int(item["id"]),
                    name=display_name(item),
                    email=item.get("emailAddress"),
                    company_id=item.get("companyID"),
                    active=bool(item.get("isActive", True)),
                    raw=item,
                )
            )

        for item in tickets:
            ticket_id = int(item["id"])
            changed_ticket_ids.add(ticket_id)
            db.merge(
                Ticket(
                    id=ticket_id,
                    ticket_number=item.get("ticketNumber"),
                    title=item.get("title") or "",
                    description=item.get("description") or "",
                    resolution=item.get("resolution") or "",
                    company_id=item.get("companyID"),
                    company_name=(
                        company_names.get(int(item["companyID"]))
                        if item.get("companyID")
                        else None
                    ),
                    assigned_resource_id=item.get("assignedResourceID"),
                    assigned_to=(
                        resource_names.get(int(item["assignedResourceID"]))
                        if item.get("assignedResourceID")
                        else None
                    ),
                    creator_resource_id=item.get("creatorResourceID"),
                    created_by=(
                        resource_names.get(int(item["creatorResourceID"]))
                        if item.get("creatorResourceID")
                        else None
                    ),
                    contact_id=item.get("contactID"),
                    create_date=dt(item.get("createDate")),
                    last_activity_date=dt(item.get("lastActivityDate")),
                    completed_date=dt(item.get("completedDate")),
                    status_id=item.get("status"),
                    priority_id=item.get("priority"),
                    raw=item,
                )
            )

        for item in notes:
            ticket_id = item.get("ticketID") or item.get("parentID")
            if not ticket_id:
                continue
            ticket_id = int(ticket_id)
            changed_ticket_ids.add(ticket_id)
            db.merge(
                TicketNote(
                    id=int(item["id"]),
                    ticket_id=ticket_id,
                    title=item.get("title") or "",
                    description=(
                        item.get("description")
                        or item.get("noteText")
                        or ""
                    ),
                    creator_resource_id=item.get("creatorResourceID"),
                    creator_name=(
                        resource_names.get(int(item["creatorResourceID"]))
                        if item.get("creatorResourceID")
                        else None
                    ),
                    create_date=dt(
                        item.get("createDateTime")
                        or item.get("createDate")
                    ),
                    note_type=item.get("noteType"),
                    publish=item.get("publish"),
                    raw=item,
                )
            )

        db.commit()

        if full:
            tickets_to_process = db.scalars(select(Ticket)).all()
        elif changed_ticket_ids:
            tickets_to_process = db.scalars(
                select(Ticket).where(Ticket.id.in_(changed_ticket_ids))
            ).all()
        else:
            tickets_to_process = []

        pending = []
        for ticket in tickets_to_process:
            ticket_notes = list(ticket.notes)
            # Use Autotask's actual resolution field. Only inspect notes
            # when the ticket genuinely has no resolution recorded.
            if not (ticket.resolution or "").strip():
                ticket.resolution = resolution_from(
                    [
                        {"title": note.title, "description": note.description}
                        for note in ticket_notes
                    ],
                    "",
                )
            ticket.document_text = build_document(ticket, ticket_notes)
            digest = sha256(ticket.document_text.encode("utf-8")).hexdigest()
            if digest != ticket.embedding_hash:
                pending.append((ticket, digest))

        db.commit()

        for batch_start in range(0, len(pending), 16):
            batch = pending[batch_start : batch_start + 16]
            vectors = embed([ticket.document_text for ticket, _ in batch])
            for (ticket, digest), vector in zip(batch, vectors):
                ticket.embedding = vector
                ticket.embedding_hash = digest
            db.commit()

        value = {
            "status": "complete",
            "mode": sync_mode,
            "companiesRefreshed": len(companies),
            "resourcesRefreshed": len(resources),
            "contactsRefreshed": len(contacts),
            "ticketsChanged": len(tickets),
            "notesChanged": len(notes),
            "embeddingsChanged": len(pending),
            "completedAt": sync_started.isoformat(),
        }

        state = db.get(SyncState, "autotask")
        if state:
            state.value = value
        else:
            db.add(SyncState(key="autotask", value=value))
        db.commit()

    return value
