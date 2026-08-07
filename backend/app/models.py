from __future__ import annotations
from datetime import datetime
from uuid import uuid4
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, 
    Integer, String, Text, func, literal_column
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from .db import Base

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    username: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    email: Mapped[str | None] = mapped_column(String(500), index=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_number: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    company_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    company_name: Mapped[str | None] = mapped_column(String(500), index=True)
    assigned_resource_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(500), index=True)
    creator_resource_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str | None] = mapped_column(String(500), index=True)
    contact_id: Mapped[int | None] = mapped_column(BigInteger)
    create_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_activity_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_id: Mapped[int | None] = mapped_column(Integer)
    priority_id: Mapped[int | None] = mapped_column(Integer)
    document_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    embedding_hash: Mapped[str | None] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    notes: Mapped[list["TicketNote"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )

Index(
    "ix_tickets_fts",
    func.to_tsvector(
        literal_column("'english'::regconfig"),
        Ticket.document_text,
),
    postgresql_using="gin",
)

class TicketNote(Base):
    __tablename__ = "ticket_notes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    creator_resource_id: Mapped[int | None] = mapped_column(BigInteger)
    creator_name: Mapped[str | None] = mapped_column(String(500))
    create_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note_type: Mapped[int | None] = mapped_column(Integer)
    publish: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    ticket: Mapped[Ticket] = relationship(back_populates="notes")

class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    user_email: Mapped[str] = mapped_column(String(500), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_email: Mapped[str] = mapped_column(String(500), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    conversation: Mapped[Conversation] = relationship(back_populates="messages")

class SyncState(Base):
    __tablename__ = "sync_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RetrievalLog(Base):
    __tablename__ = "retrieval_logs"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    user_email: Mapped[str] = mapped_column(String(500), index=True)
    question: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(50))
    engine: Mapped[str] = mapped_column(String(100))
    matched_ticket_ids: Mapped[list] = mapped_column(JSONB, default=list)
    search_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
