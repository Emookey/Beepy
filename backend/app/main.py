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

    mode = body.mode.lower()
    tickets = []
    if mode in {"smart", "tickets"}:
        t0 = time.perf_counter()
        tickets = search_tickets(question)
        print(
        f"Ticket search took "
        f"{(time.perf_counter() - t0):.3f} seconds",
        flush=True,
   )

    if mode == "tickets":
        answer = answer_ticket_question(question, tickets, history)
        engine = "autotask-hybrid"
    elif mode == "tech":
        answer = answer_tech_question(question, history)
        engine = "qwen-tech"
    else:
        if tickets:
            answer = answer_ticket_question(question, tickets, history)
            engine = "autotask-hybrid"
        else:
            answer = answer_tech_question(question, history)
            engine = "qwen-tech-fallback"

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

    mode = body.mode.lower()
    started = time.perf_counter()
    tickets = search_tickets(question) if mode in {"smart", "tickets"} else []

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
        answer_parts = []

        if tickets:
            engine = "autotask-exact"
        elif mode == "tickets":
            engine = "autotask-no-match"
        else:
            engine = "qwen-tech-stream"

        yield f"event: meta\ndata: {json.dumps({
            'conversationId': conversation_id,
            'sources': sources,
            'engine': engine,
            'matchedTickets': len(tickets),
        })}\n\n"

        # Matching ticket requests never pass through Qwen. This makes them fast
        # and prevents the model from rewriting exact Autotask fields.
        if tickets:
            answer = answer_ticket_question(question, tickets, history)
            answer_parts.append(answer)
            yield f"event: token\ndata: {json.dumps({'text': answer})}\n\n"
        elif mode == "tickets":
            answer = (
                "## No matching Autotask tickets\n\n"
                "I searched the synchronized Autotask index but did not find a match."
            )
            answer_parts.append(answer)
            yield f"event: token\ndata: {json.dumps({'text': answer})}\n\n"
        else:
            tech_messages = [{
                "role": "system",
                "content": """ /no_think
You are MBC - Beepy, a senior MSP support technician.
Give technically accurate, practical troubleshooting guidance.
Separate confirmed facts from likely causes, explain what each diagnostic step proves,
and ask for missing product, version, error, or topology information when necessary.
""",
            }, *history[-8:], {"role": "user", "content": question}]
            try:
                for token in chat_stream(tech_messages, temperature=0.15):
                    answer_parts.append(token)
                    yield f"event: token\ndata: {json.dumps({'text': token})}\n\n"
            except Exception:
                fallback = answer_tech_question(question, history)
                answer_parts[:] = [fallback]
                yield f"event: replace\ndata: {json.dumps({'text': fallback})}\n\n"

        answer = "".join(answer_parts)
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
                mode=mode,
                engine=engine,
                matched_ticket_ids=[ticket.id for ticket in tickets],
                search_plan={"quantityControlledByRequest": True},
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
