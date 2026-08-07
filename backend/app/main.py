from pathlib import Path
from uuid import uuid4
import time
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from .auth import User, require_user
from .config import get_settings
from .db import SessionLocal, initialize_database
from .models import Conversation, Message, SyncState, Ticket, TicketNote, RetrievalLog
from .search import answer_tech_question, answer_ticket_question, search_tickets
from .ollama import chat_stream
from .odysseus import OdysseusError, answer_odysseus_tech
from .autotask import AutotaskClient
from .interpreter import interpret_question

settings = get_settings()
autotask_client = AutotaskClient()
app = FastAPI(title=settings.app_name)

class ChatRequest(BaseModel):
    question: str
    mode: str = "smart"
    conversationId: str | None = None

@app.on_event("startup")
def startup():
    initialize_database()

@app.get("/api/health")
def health():
    return {"ok": True, "service": settings.app_name, "version": "2.0.0-goodwill"}

@app.get("/api/public-config")
def public_config():
    return {
        "clientId": settings.entra_client_id,
        "tenantId": settings.entra_tenant_id,
        "allowedDomain": settings.allowed_email_domain,
    }

@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"id": user.id, "email": user.email, "name": user.name}

@app.get("/api/status")
def status(user: User = Depends(require_user)):
    with SessionLocal() as db:
        tickets = db.scalar(select(func.count()).select_from(Ticket)) or 0
        notes = db.scalar(select(func.count()).select_from(TicketNote)) or 0
        state = db.get(SyncState, "autotask")
    return {"tickets": tickets, "notes": notes, "sync": state.value if state else {"status": "never"}}

@app.get("/api/conversations")
def list_conversations(user: User = Depends(require_user)):
    with SessionLocal() as db:
        rows = db.scalars(
            select(Conversation)
            .where(Conversation.user_email == user.email)
            .order_by(Conversation.updated_at.desc())
            .limit(100)
        ).all()
        return [{"id": x.id, "title": x.title, "updatedAt": x.updated_at} for x in rows]

@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_email == user.email,
            )
        )
        if not conversation:
            raise HTTPException(404, "Conversation not found.")
        messages = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        ).all()
        return {
            "id": conversation.id,
            "title": conversation.title,
            "messages": [
                {"role": m.role, "content": m.content, "sources": m.sources}
                for m in messages
            ],
        }

@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_email == user.email,
            )
        )
        if conversation:
            db.delete(conversation)
            db.commit()
    return {"ok": True}


@app.post("/api/interpret")
def interpret_endpoint(
    body: ChatRequest,
    user: User = Depends(require_user),
):
    """
    Shadow-mode natural-language interpretation.

    This endpoint does not search tickets and cannot modify the existing
    production ticket behavior.
    """
    question = body.question.strip()

    if not question:
        raise HTTPException(400, "Question is required.")

    interpretation = interpret_question(
        question=question,
        user_email=user.email,
    )

    return interpretation.to_dict()


def _resolve_chat_mode(requested_mode: str, question: str, user_email: str) -> tuple[str, dict]:
    """Resolve Auto before retrieval so tech questions never query tickets first."""
    requested = (requested_mode or "auto").strip().lower()
    if requested == "smart":
        requested = "auto"  # backward compatibility with an older open tab
    if requested in {"tickets", "tech"}:
        return requested, {"requested": requested, "reason": "manual"}

    interpretation = interpret_question(question=question, user_email=user_email)
    lower = question.lower()
    tech_leads = (
        "how do i", "how can i", "how to", "why does", "why is",
        "what causes", "troubleshoot", "not working", "cannot connect",
        "can't connect", "error", "crashing", "freezing",
    )
    if any(term in lower for term in tech_leads):
        return "tech", {
            "requested": "auto",
            "reason": "technical wording",
            "interpretation": interpretation.to_dict(),
        }

    if interpretation.intent == "ticket_search":
        return "tickets", {
            "requested": "auto",
            "reason": "ticket-history wording",
            "interpretation": interpretation.to_dict(),
        }

    historical = any(term in lower for term in (
        "worked", "done", "completed", "closed", "resolved", "created",
        "assigned", "history", "previous", "past", "recent", "has had",
        "have had", "did yesterday", "did today",
    ))
    if historical and (
        interpretation.technician
        or interpretation.company
        or interpretation.date_from
    ):
        return "tickets", {
            "requested": "auto",
            "reason": "historical entity/date request",
            "interpretation": interpretation.to_dict(),
        }

    return "tech", {
        "requested": "auto",
        "reason": "default technical question",
        "interpretation": interpretation.to_dict(),
    }


@app.post("/api/chat")
def chat_endpoint(body: ChatRequest, user: User = Depends(require_user)):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Question is required.")

    with SessionLocal() as db:
        conversation = None
        if body.conversationId:
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.id == body.conversationId,
                    Conversation.user_email == user.email,
                )
            )
        if not conversation:
            conversation = Conversation(
                id=str(uuid4()),
                user_email=user.email,
                title=question[:70],
            )
            db.add(conversation)
            db.flush()

        history_rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        ).all()
        history = [{"role": x.role, "content": x.content} for x in history_rows[-12:]]

        db.add(Message(
            conversation_id=conversation.id,
            user_email=user.email,
            role="user",
            content=question,
            sources=[],
        ))
        db.commit()

    requested_mode = body.mode.lower()
    mode, route_plan = _resolve_chat_mode(requested_mode, question, user.email)
    tickets = []

    if mode == "tickets":
        t0 = time.perf_counter()
        tickets = search_tickets(question)
        print(
            f"Ticket search took {(time.perf_counter() - t0):.3f} seconds",
            flush=True,
        )
        answer = answer_ticket_question(question, tickets, history)
        engine = "autotask-hybrid" if tickets else "autotask-no-match"
    else:
        try:
            answer = answer_odysseus_tech(question, history)
            engine = "odysseus-rag"
        except OdysseusError as exc:
            print(f"Odysseus tech fallback: {exc}", flush=True)
            try:
                answer = answer_tech_question(question, history)
                engine = "local-qwen-fallback"
            except Exception as fallback_exc:
                print(f"Local tech fallback also failed: {fallback_exc}", flush=True)
                answer = (
                    "## Tech service unavailable\n\n"
                    "Odysseus could not answer this request, and the local fallback model "
                    "was also unavailable. Ticket Search is still available."
                )
                engine = "tech-unavailable"

    sources = [
        {
            "ticketNumber": t.ticket_number,
            "title": t.title,
            "company": t.company_name,
            "createdDate": str(t.create_date or ""),
            "url": autotask_client.ticket_web_url(t.id),
        }
        for t in tickets
    ]

    with SessionLocal() as db:
        db.add(Message(
            conversation_id=conversation.id,
            user_email=user.email,
            role="assistant",
            content=answer,
            sources=sources,
        ))
        db.commit()

    return {
        "answer": answer,
        "conversationId": conversation.id,
        "sources": sources,
        "engine": engine,
        "matchedTickets": len(tickets),
        "resolvedMode": mode,
    }


@app.post("/api/chat/stream")
def chat_stream_endpoint(body: ChatRequest, user: User = Depends(require_user)):
    import json

    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Question is required.")

    with SessionLocal() as db:
        conversation = None
        if body.conversationId:
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.id == body.conversationId,
                    Conversation.user_email == user.email,
                )
            )
        if not conversation:
            conversation = Conversation(
                id=str(uuid4()),
                user_email=user.email,
                title=question[:70],
            )
            db.add(conversation)
            db.flush()

        history_rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        ).all()
        history = [{"role": row.role, "content": row.content} for row in history_rows[-12:]]
        db.add(Message(
            conversation_id=conversation.id,
            user_email=user.email,
            role="user",
            content=question,
            sources=[],
        ))
        db.commit()
        conversation_id = conversation.id

    requested_mode = body.mode.lower()
    mode, route_plan = _resolve_chat_mode(requested_mode, question, user.email)
    started = time.perf_counter()
    tickets = search_tickets(question) if mode == "tickets" else []

    sources = [
        {
            "ticketNumber": ticket.ticket_number,
            "title": ticket.title,
            "company": ticket.company_name,
            "createdDate": str(ticket.create_date or ""),
            "url": autotask_client.ticket_web_url(ticket.id),
        }
        for ticket in tickets
    ]

    def event_stream():
        if mode == "tickets":
            engine = "autotask-exact" if tickets else "autotask-no-match"
            answer = answer_ticket_question(question, tickets, history)
        else:
            try:
                answer = answer_odysseus_tech(question, history)
                engine = "odysseus-rag"
            except OdysseusError as exc:
                print(f"Odysseus tech fallback: {exc}", flush=True)
                try:
                    answer = answer_tech_question(question, history)
                    engine = "local-qwen-fallback"
                except Exception as fallback_exc:
                    print(f"Local tech fallback also failed: {fallback_exc}", flush=True)
                    answer = (
                        "## Tech service unavailable\n\n"
                        "Odysseus could not answer this request, and the local fallback model "
                        "was also unavailable. Ticket Search is still available."
                    )
                    engine = "tech-unavailable"

        yield f"event: meta\ndata: {json.dumps({
            'conversationId': conversation_id,
            'sources': sources,
            'engine': engine,
            'matchedTickets': len(tickets),
            'resolvedMode': mode,
        })}\n\n"
        yield f"event: token\ndata: {json.dumps({'text': answer})}\n\n"

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        with SessionLocal() as db:
            db.add(Message(
                conversation_id=conversation_id,
                user_email=user.email,
                role="assistant",
                content=answer,
                sources=sources,
            ))
            db.add(RetrievalLog(
                user_email=user.email,
                question=question,
                mode=requested_mode,
                engine=engine,
                matched_ticket_ids=[ticket.id for ticket in tickets],
                search_plan={
                    "quantityControlledByRequest": True,
                    "resolvedMode": mode,
                    "route": route_plan,
                },
                elapsed_ms=elapsed_ms,
            ))
            db.commit()
        yield f"event: done\ndata: {json.dumps({'elapsedMs': elapsed_ms})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/retrieval/recent")
def recent_retrievals(user: User = Depends(require_user)):
    with SessionLocal() as db:
        rows = db.scalars(
            select(RetrievalLog)
            .where(RetrievalLog.user_email == user.email)
            .order_by(RetrievalLog.created_at.desc())
            .limit(50)
        ).all()
    return [
        {
            "question": row.question,
            "mode": row.mode,
            "engine": row.engine,
            "matchedTicketIds": row.matched_ticket_ids,
            "elapsedMs": row.elapsed_ms,
            "createdAt": row.created_at,
        }
        for row in rows
    ]


static_dir = Path("/app/static")

@app.get("/{path:path}")
def spa(path: str):
    requested = static_dir / path
    if path and requested.is_file():
        return FileResponse(requested)
    return FileResponse(static_dir / "index.html")
