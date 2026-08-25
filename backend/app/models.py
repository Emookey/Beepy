from __future__ import annotations
from datetime import datetime
from uuid import uuid4
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, 
    Integer, String, Text, UniqueConstraint, func, literal_column
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


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "email", name="uq_project_member_email"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(500), index=True)
    role: Mapped[str] = mapped_column(String(50), default="member")
    invited_by_email: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    project: Mapped[Project] = relationship(back_populates="members")


# --- Project Workspace v3 -------------------------------------------------

class ProjectWorkspaceSetting(Base):
    __tablename__ = "project_workspace_settings"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(40), default="active")
    client_name: Mapped[str] = mapped_column(String(300), default="")
    layout: Mapped[list] = mapped_column(JSONB, default=list)
    updated_by_email: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProjectMessage(Base):
    __tablename__ = "project_messages"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(30), default="team", index=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    author_email: Mapped[str] = mapped_column(String(500), index=True)
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectNote(Base):
    __tablename__ = "project_notes"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), default="Note")
    content: Mapped[str] = mapped_column(Text, default="")
    folder: Mapped[str] = mapped_column(String(120), default="General", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_type: Mapped[str | None] = mapped_column(String(50))
    source_id: Mapped[str | None] = mapped_column(String(100))
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProjectLink(Base):
    __tablename__ = "project_links"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ProjectTask(Base):
    __tablename__ = "project_tasks"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(30), default="normal")
    assignee_email: Mapped[str | None] = mapped_column(String(500), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProjectIdea(Base):
    __tablename__ = "project_ideas"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="discussing", index=True)
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProjectIdeaVote(Base):
    __tablename__ = "project_idea_votes"
    __table_args__ = (
        UniqueConstraint("idea_id", "email", name="uq_project_idea_vote_email"),
    )
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    idea_id: Mapped[str] = mapped_column(
        ForeignKey("project_ideas.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(500), index=True)
    vote: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectDecision(Base):
    __tablename__ = "project_decisions"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(400))
    decision: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ProjectRisk(Base):
    __tablename__ = "project_risks"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(400))
    impact: Mapped[str] = mapped_column(String(30), default="medium")
    likelihood: Mapped[str] = mapped_column(String(30), default="medium")
    mitigation: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="open", index=True)
    created_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ProjectFile(Base):
    __tablename__ = "project_files"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    stored_name: Mapped[str] = mapped_column(String(500), unique=True)
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_by_email: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ProjectActivity(Base):
    __tablename__ = "project_activity"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    actor_email: Mapped[str] = mapped_column(String(500), index=True)
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --- Microsoft 365 Email Intelligence ------------------------------------

class EmailMailbox(Base):
    __tablename__ = "email_mailboxes"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    graph_user_id: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    primary_address: Mapped[str] = mapped_column(String(500), index=True)
    user_principal_name: Mapped[str] = mapped_column(String(500), index=True)
    display_name: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailFolder(Base):
    __tablename__ = "email_folders"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "graph_folder_id", name="uq_email_folder_graph_id"),
    )
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    mailbox_id: Mapped[str] = mapped_column(
        ForeignKey("email_mailboxes.id", ondelete="CASCADE"), index=True
    )
    graph_folder_id: Mapped[str] = mapped_column(String(1000))
    display_name: Mapped[str] = mapped_column(String(300), default="", index=True)
    parent_graph_folder_id: Mapped[str | None] = mapped_column(String(1000))
    total_item_count: Mapped[int] = mapped_column(Integer, default=0)
    delta_link: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "graph_message_id", name="uq_email_message_mailbox_graph"),
    )
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    mailbox_id: Mapped[str] = mapped_column(
        ForeignKey("email_mailboxes.id", ondelete="CASCADE"), index=True
    )
    folder_id: Mapped[str] = mapped_column(
        ForeignKey("email_folders.id", ondelete="CASCADE"), index=True
    )
    graph_message_id: Mapped[str] = mapped_column(String(1200))
    internet_message_id: Mapped[str | None] = mapped_column(String(1000), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(500), index=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    sender_name: Mapped[str] = mapped_column(String(500), default="", index=True)
    sender_address: Mapped[str] = mapped_column(String(500), default="", index=True)
    to_recipients: Mapped[list] = mapped_column(JSONB, default=list)
    cc_recipients: Mapped[list] = mapped_column(JSONB, default=list)
    bcc_recipients: Mapped[list] = mapped_column(JSONB, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_preview: Mapped[str] = mapped_column(Text, default="")
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    importance: Mapped[str] = mapped_column(String(30), default="normal")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    web_link: Mapped[str] = mapped_column(Text, default="")
    document_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    embedding_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


Index(
    "ix_email_messages_fts",
    func.to_tsvector(
        literal_column("'english'::regconfig"),
        EmailMessage.document_text,
    ),
    postgresql_using="gin",
)


class EmailSearchAudit(Base):
    __tablename__ = "email_search_audit"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    user_email: Mapped[str] = mapped_column(String(500), index=True)
    question: Mapped[str] = mapped_column(Text)
    matched_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
