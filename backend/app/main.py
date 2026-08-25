from pathlib import Path
import os
import re
import shutil
from urllib.parse import urlparse
from datetime import datetime, timezone
from uuid import uuid4
import time
from fastapi import Depends, FastAPI, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from .auth import User, require_user
from .config import get_settings
from .db import SessionLocal, initialize_database
from .models import (
    Conversation, Message, SyncState, Ticket, TicketNote, RetrievalLog,
    Project, ProjectMember, ProjectWorkspaceSetting, ProjectMessage, ProjectNote,
    ProjectLink, ProjectTask, ProjectIdea, ProjectIdeaVote, ProjectDecision,
    ProjectRisk, ProjectFile, ProjectActivity,
    EmailMailbox, EmailFolder, EmailMessage, EmailSearchAudit,
)
from .search import answer_tech_question, answer_ticket_question, search_tickets
from .ollama import chat_stream
from .odysseus import OdysseusError, answer_odysseus_tech
from .autotask import AutotaskClient
from .interpreter import interpret_question
from .email_access import can_search_tenant_email
from .email_graph import email_indexer_configured
from .email_search import search_emails, answer_email_question, audit_email_search

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
    try:
        PROJECT_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

@app.get("/api/health")
def health():
    return {"ok": True, "service": settings.app_name, "version": "2.1.0-email-intelligence-v1"}

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
        email_state = db.get(SyncState, "email")
        emails = int((email_state.value or {}).get("indexedMessages") or 0) if email_state and can_search_tenant_email(user.email) else 0
    return {
        "tickets": tickets,
        "notes": notes,
        "emails": emails,
        "sync": state.value if state else {"status": "never"},
    }


@app.get("/api/email/status")
def email_status(user: User = Depends(require_user)):
    authorized = can_search_tenant_email(user.email)
    with SessionLocal() as db:
        sync = db.get(SyncState, "email")
        if authorized:
            messages = int((sync.value or {}).get("indexedMessages") or 0) if sync else 0
            mailboxes = db.scalar(select(func.count()).select_from(EmailMailbox)) or 0
            folders = db.scalar(select(func.count()).select_from(EmailFolder)) or 0
        else:
            messages = mailboxes = folders = 0
    return {
        "authorized": authorized,
        "configured": email_indexer_configured(),
        "messages": int(messages),
        "mailboxes": int(mailboxes),
        "folders": int(folders),
        "sync": sync.value if sync else {"status": "never"},
    }


@app.get("/api/email/search")
def email_search_endpoint(q: str, limit: int = 25, user: User = Depends(require_user)):
    if not can_search_tenant_email(user.email):
        raise HTTPException(403, "Tenant Email Intelligence is not enabled for your Beepy account.")
    started = time.perf_counter()
    hits = search_emails(q, limit=limit)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    audit_email_search(user.email, q, hits, elapsed_ms)
    return {
        "count": len(hits),
        "results": [
            {
                "id": h.id,
                "subject": h.subject,
                "senderName": h.sender_name,
                "senderAddress": h.sender_address,
                "to": h.to_recipients,
                "cc": h.cc_recipients,
                "sentAt": h.sent_at,
                "receivedAt": h.received_at,
                "preview": h.body_preview or h.body_text[:1000],
                "hasAttachments": h.has_attachments,
                "mailbox": h.mailbox,
                "folder": h.folder,
                "webLink": h.web_link,
            } for h in hits
        ],
    }

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

@app.delete("/api/conversations")
def clear_conversations(user: User = Depends(require_user)):
    """Delete only the signed-in user's Beepy conversation history."""
    with SessionLocal() as db:
        rows = db.scalars(
            select(Conversation).where(Conversation.user_email == user.email)
        ).all()
        deleted = len(rows)
        for conversation in rows:
            db.delete(conversation)
        db.commit()
    return {"ok": True, "deleted": deleted}


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


# --- Project Workspace v3 -------------------------------------------------

PROJECT_UPLOAD_ROOT = Path(os.getenv("PROJECT_UPLOAD_ROOT", "/app/project_uploads"))
PROJECT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PROJECT_DEFAULT_LAYOUT = [
    "brief", "beepy", "chat", "notes", "tasks", "ideas", "risks", "links", "files", "decisions", "activity"
]
PROJECT_ROLES = {"owner", "admin", "member"}
PROJECT_STATUSES = {"planning", "active", "waiting", "blocked", "completed", "archived"}


class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectInviteRequest(BaseModel):
    email: str
    role: str = "member"


class ProjectRoleRequest(BaseModel):
    role: str


class ProjectWorkspaceSettingsRequest(BaseModel):
    status: str | None = None
    clientName: str | None = None
    layout: list[str] | None = None


class ProjectMessageRequest(BaseModel):
    content: str


class ProjectNoteRequest(BaseModel):
    title: str = "Note"
    content: str
    folder: str = "General"
    pinned: bool = False
    sourceType: str | None = None
    sourceId: str | None = None


class ProjectNoteUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    folder: str | None = None
    pinned: bool | None = None


class ProjectLinkRequest(BaseModel):
    title: str
    url: str
    description: str = ""


class ProjectTaskRequest(BaseModel):
    title: str
    description: str = ""
    status: str = "open"
    priority: str = "normal"
    assigneeEmail: str | None = None
    dueAt: datetime | None = None


class ProjectTaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assigneeEmail: str | None = None
    dueAt: datetime | None = None


class ProjectIdeaRequest(BaseModel):
    title: str
    description: str = ""
    status: str = "discussing"


class ProjectIdeaUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None


class ProjectIdeaVoteRequest(BaseModel):
    vote: int


class ProjectDecisionRequest(BaseModel):
    title: str
    decision: str
    rationale: str = ""


class ProjectRiskRequest(BaseModel):
    title: str
    impact: str = "medium"
    likelihood: str = "medium"
    mitigation: str = ""
    status: str = "open"


class ProjectRiskUpdateRequest(BaseModel):
    title: str | None = None
    impact: str | None = None
    likelihood: str | None = None
    mitigation: str | None = None
    status: str | None = None


class ProjectBeepyRequest(BaseModel):
    question: str


def _norm_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _clean_role(value: str | None) -> str:
    role = str(value or "member").strip().lower()
    return role if role in PROJECT_ROLES else "member"


def _project_role(db, project: Project, user_email: str) -> str | None:
    email = _norm_email(user_email)
    if _norm_email(project.owner_email) == email:
        return "owner"
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            func.lower(ProjectMember.email) == email,
        )
    )
    return _clean_role(member.role) if member else None


def _project_for_user(db, project_id: str, user_email: str) -> Project | None:
    project = db.get(Project, project_id)
    if not project:
        return None
    return project if _project_role(db, project, user_email) else None


def _require_project(db, project_id: str, user_email: str, allowed_roles: set[str] | None = None):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found.")
    role = _project_role(db, project, user_email)
    if not role:
        raise HTTPException(404, "Project not found.")
    if allowed_roles and role not in allowed_roles:
        raise HTTPException(403, "Your project role does not allow that change.")
    return project, role


def _project_permissions(role: str) -> dict:
    role = _clean_role(role)
    return {
        "manageWorkspace": role in {"owner", "admin"},
        "manageMembers": role in {"owner", "admin"},
        "manageRoles": role == "owner",
        "deleteProject": role == "owner",
        "manageDecisions": role in {"owner", "admin"},
    }


def _project_summary(db, project: Project, user_email: str) -> dict:
    member_count = db.scalar(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == project.id
        )
    ) or 0
    role = _project_role(db, project, user_email) or "member"
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description or "",
        "ownerEmail": project.owner_email,
        "role": role,
        "permissions": _project_permissions(role),
        "memberCount": int(member_count) + 1,
        "createdAt": project.created_at,
        "updatedAt": project.updated_at,
    }


def _project_members_payload(db, project: Project) -> list[dict]:
    members = db.scalars(
        select(ProjectMember)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.created_at)
    ).all()
    payload = [{
        "id": "owner",
        "email": project.owner_email,
        "role": "owner",
        "primaryOwner": True,
        "invitedBy": project.owner_email,
    }]
    for member in members:
        if _norm_email(member.email) == _norm_email(project.owner_email):
            continue
        payload.append({
            "id": member.id,
            "email": member.email,
            "role": _clean_role(member.role),
            "primaryOwner": False,
            "invitedBy": member.invited_by_email,
        })
    return payload


def _workspace_setting(db, project_id: str, create: bool = False) -> ProjectWorkspaceSetting:
    setting = db.get(ProjectWorkspaceSetting, project_id)
    if setting:
        return setting
    setting = ProjectWorkspaceSetting(
        project_id=project_id,
        status="active",
        client_name="",
        layout=list(PROJECT_DEFAULT_LAYOUT),
    )
    if create:
        db.add(setting)
        db.flush()
    return setting


def _activity(db, project_id: str, actor_email: str, action: str, entity_type: str | None = None, entity_id: str | None = None, details: dict | None = None):
    db.add(ProjectActivity(
        id=str(uuid4()),
        project_id=project_id,
        actor_email=_norm_email(actor_email),
        action=action[:120],
        entity_type=(entity_type or None),
        entity_id=(entity_id or None),
        details=details or {},
    ))


def _valid_http_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "Link must be a valid http:// or https:// URL.")
    return url[:4000]


def _project_health(tasks: list[ProjectTask], risks: list[ProjectRisk]) -> dict:
    open_tasks = sum(1 for x in tasks if x.status not in {"done", "completed"})
    blocked = sum(1 for x in tasks if x.status == "blocked")
    open_risks = sum(1 for x in risks if x.status != "closed")
    high_risks = sum(1 for x in risks if x.status != "closed" and x.impact == "high")
    if blocked or high_risks >= 2:
        state = "at-risk"
    elif open_risks or open_tasks:
        state = "needs-attention"
    else:
        state = "healthy"
    return {"state": state, "openTasks": open_tasks, "blockedTasks": blocked, "openRisks": open_risks, "highRisks": high_risks}


def _workspace_payload(db, project: Project, user_email: str) -> dict:
    setting = _workspace_setting(db, project.id, create=False)
    team_messages = db.scalars(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.channel == "team",
        ).order_by(ProjectMessage.created_at.desc()).limit(200)
    ).all()[::-1]
    beepy_messages = db.scalars(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.channel == "beepy",
        ).order_by(ProjectMessage.created_at.desc()).limit(100)
    ).all()[::-1]
    notes = db.scalars(select(ProjectNote).where(ProjectNote.project_id == project.id).order_by(ProjectNote.pinned.desc(), ProjectNote.updated_at.desc())).all()
    links = db.scalars(select(ProjectLink).where(ProjectLink.project_id == project.id).order_by(ProjectLink.created_at.desc())).all()
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id).order_by(ProjectTask.updated_at.desc())).all()
    ideas = db.scalars(select(ProjectIdea).where(ProjectIdea.project_id == project.id).order_by(ProjectIdea.updated_at.desc())).all()
    decisions = db.scalars(select(ProjectDecision).where(ProjectDecision.project_id == project.id).order_by(ProjectDecision.created_at.desc())).all()
    risks = db.scalars(select(ProjectRisk).where(ProjectRisk.project_id == project.id).order_by(ProjectRisk.updated_at.desc())).all()
    files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id).order_by(ProjectFile.created_at.desc())).all()
    activity = db.scalars(select(ProjectActivity).where(ProjectActivity.project_id == project.id).order_by(ProjectActivity.created_at.desc()).limit(120)).all()

    votes = db.execute(
        select(ProjectIdeaVote.idea_id, func.sum(ProjectIdeaVote.vote)).where(
            ProjectIdeaVote.idea_id.in_([x.id for x in ideas] or ["00000000-0000-0000-0000-000000000000"])
        ).group_by(ProjectIdeaVote.idea_id)
    ).all()
    vote_map = {idea_id: int(total or 0) for idea_id, total in votes}
    my_votes = db.scalars(
        select(ProjectIdeaVote).where(
            ProjectIdeaVote.idea_id.in_([x.id for x in ideas] or ["00000000-0000-0000-0000-000000000000"]),
            func.lower(ProjectIdeaVote.email) == _norm_email(user_email),
        )
    ).all()
    my_vote_map = {x.idea_id: x.vote for x in my_votes}

    def msg(x):
        return {"id":x.id,"channel":x.channel,"role":x.role,"authorEmail":x.author_email,"content":x.content,"sources":x.sources or [],"createdAt":x.created_at,"editedAt":x.edited_at}
    return {
        "settings": {
            "status": setting.status or "active",
            "clientName": setting.client_name or "",
            "layout": setting.layout or list(PROJECT_DEFAULT_LAYOUT),
        },
        "health": _project_health(tasks, risks),
        "teamMessages": [msg(x) for x in team_messages],
        "beepyMessages": [msg(x) for x in beepy_messages],
        "notes": [{"id":x.id,"title":x.title,"content":x.content,"folder":x.folder,"pinned":x.pinned,"sourceType":x.source_type,"sourceId":x.source_id,"createdByEmail":x.created_by_email,"createdAt":x.created_at,"updatedAt":x.updated_at} for x in notes],
        "links": [{"id":x.id,"title":x.title,"url":x.url,"description":x.description,"createdByEmail":x.created_by_email,"createdAt":x.created_at} for x in links],
        "tasks": [{"id":x.id,"title":x.title,"description":x.description,"status":x.status,"priority":x.priority,"assigneeEmail":x.assignee_email,"dueAt":x.due_at,"createdByEmail":x.created_by_email,"createdAt":x.created_at,"updatedAt":x.updated_at} for x in tasks],
        "ideas": [{"id":x.id,"title":x.title,"description":x.description,"status":x.status,"createdByEmail":x.created_by_email,"createdAt":x.created_at,"updatedAt":x.updated_at,"score":vote_map.get(x.id,0),"myVote":my_vote_map.get(x.id,0)} for x in ideas],
        "decisions": [{"id":x.id,"title":x.title,"decision":x.decision,"rationale":x.rationale,"createdByEmail":x.created_by_email,"createdAt":x.created_at} for x in decisions],
        "risks": [{"id":x.id,"title":x.title,"impact":x.impact,"likelihood":x.likelihood,"mitigation":x.mitigation,"status":x.status,"createdByEmail":x.created_by_email,"createdAt":x.created_at,"updatedAt":x.updated_at} for x in risks],
        "files": [{"id":x.id,"filename":x.filename,"contentType":x.content_type,"sizeBytes":x.size_bytes,"uploadedByEmail":x.uploaded_by_email,"createdAt":x.created_at,"downloadUrl":f"/api/projects/{project.id}/files/{x.id}/download"} for x in files],
        "activity": [{"id":x.id,"actorEmail":x.actor_email,"action":x.action,"entityType":x.entity_type,"entityId":x.entity_id,"details":x.details or {},"createdAt":x.created_at} for x in activity],
    }


@app.get("/api/projects")
def list_projects(user: User = Depends(require_user)):
    with SessionLocal() as db:
        rows = db.scalars(select(Project).order_by(Project.updated_at.desc())).all()
        return [_project_summary(db, project, user.email) for project in rows if _project_role(db, project, user.email)]


@app.post("/api/projects")
def create_project(body: ProjectCreateRequest, user: User = Depends(require_user)):
    name = body.name.strip()
    description = body.description.strip()
    if not name:
        raise HTTPException(400, "Project name is required.")
    if len(name) > 200:
        raise HTTPException(400, "Project name is too long.")
    with SessionLocal() as db:
        project = Project(id=str(uuid4()), name=name, description=description[:4000], owner_email=_norm_email(user.email))
        db.add(project)
        db.flush()
        _workspace_setting(db, project.id, create=True)
        _activity(db, project.id, user.email, "created the project", "project", project.id, {"name": name})
        db.commit()
        db.refresh(project)
        return _project_summary(db, project, user.email)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, role = _require_project(db, project_id, user.email)
        return {**_project_summary(db, project, user.email), "members": _project_members_payload(db, project)}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdateRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email, {"owner", "admin"})
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(400, "Project name is required.")
            project.name = name[:200]
        if body.description is not None:
            project.description = body.description.strip()[:4000]
        project.updated_at = datetime.now(timezone.utc)
        _activity(db, project.id, user.email, "updated project details", "project", project.id)
        db.commit()
        return _project_summary(db, project, user.email)


@app.post("/api/projects/{project_id}/invite")
def invite_project_member(project_id: str, body: ProjectInviteRequest, user: User = Depends(require_user)):
    email = _norm_email(body.email)
    role = _clean_role(body.role)
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")
    domain = email.rsplit("@", 1)[-1]
    if domain != settings.allowed_email_domain.lower():
        raise HTTPException(400, f"Project members must use @{settings.allowed_email_domain} accounts.")
    with SessionLocal() as db:
        project, caller_role = _require_project(db, project_id, user.email, {"owner", "admin"})
        if caller_role == "admin" and role != "member":
            raise HTTPException(403, "Admins can invite members. Owners assign Admin/Owner roles.")
        if email == _norm_email(project.owner_email):
            raise HTTPException(400, "The primary owner is already in the project.")
        existing = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id, func.lower(ProjectMember.email) == email))
        if existing:
            return {"ok": True, "alreadyMember": True, "memberId": existing.id}
        member = ProjectMember(id=str(uuid4()), project_id=project.id, email=email, role=role, invited_by_email=_norm_email(user.email))
        db.add(member)
        project.updated_at = datetime.now(timezone.utc)
        _activity(db, project.id, user.email, f"invited {email} as {role}", "member", member.id, {"email":email,"role":role})
        db.commit()
        return {"ok": True, "memberId": member.id}


@app.patch("/api/projects/{project_id}/members/{member_id}/role")
def change_project_member_role(project_id: str, member_id: str, body: ProjectRoleRequest, user: User = Depends(require_user)):
    role = _clean_role(body.role)
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email, {"owner"})
        member = db.scalar(select(ProjectMember).where(ProjectMember.id == member_id, ProjectMember.project_id == project.id))
        if not member:
            raise HTTPException(404, "Project member not found.")
        member.role = role
        _activity(db, project.id, user.email, f"changed {member.email} to {role}", "member", member.id, {"email":member.email,"role":role})
        db.commit()
        return {"ok": True, "role": role}


@app.delete("/api/projects/{project_id}/members/{member_id}")
def remove_project_member(project_id: str, member_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, caller_role = _require_project(db, project_id, user.email)
        member = db.scalar(select(ProjectMember).where(ProjectMember.id == member_id, ProjectMember.project_id == project.id))
        if not member:
            raise HTTPException(404, "Project member not found.")
        self_leave = _norm_email(member.email) == _norm_email(user.email)
        if not self_leave and caller_role not in {"owner", "admin"}:
            raise HTTPException(403, "Only Owners/Admins can remove other members.")
        if caller_role == "admin" and _clean_role(member.role) in {"owner", "admin"} and not self_leave:
            raise HTTPException(403, "Admins cannot remove Owners or other Admins.")
        removed_email = member.email
        db.delete(member)
        _activity(db, project.id, user.email, f"removed {removed_email} from the project", "member", member_id, {"email":removed_email})
        db.commit()
        return {"ok": True}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email, {"owner"})
        db.delete(project)
        db.commit()
    shutil.rmtree(PROJECT_UPLOAD_ROOT / project_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/projects/{project_id}/workspace")
def get_project_workspace(project_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        return _workspace_payload(db, project, user.email)


@app.patch("/api/projects/{project_id}/workspace/settings")
def update_project_workspace_settings(project_id: str, body: ProjectWorkspaceSettingsRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email, {"owner", "admin"})
        setting = _workspace_setting(db, project.id, create=True)
        if body.status is not None:
            status = body.status.strip().lower()
            if status not in PROJECT_STATUSES:
                raise HTTPException(400, "Invalid project status.")
            setting.status = status
        if body.clientName is not None:
            setting.client_name = body.clientName.strip()[:300]
        if body.layout is not None:
            clean = []
            for item in body.layout:
                item = str(item).strip().lower()
                if item in PROJECT_DEFAULT_LAYOUT and item not in clean:
                    clean.append(item)
            setting.layout = clean or list(PROJECT_DEFAULT_LAYOUT)
        setting.updated_by_email = _norm_email(user.email)
        project.updated_at = datetime.now(timezone.utc)
        _activity(db, project.id, user.email, "updated the project workspace", "workspace", project.id)
        db.commit()
        return {"ok": True, "settings": {"status":setting.status,"clientName":setting.client_name,"layout":setting.layout}}


@app.post("/api/projects/{project_id}/messages")
def create_project_message(project_id: str, body: ProjectMessageRequest, user: User = Depends(require_user)):
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Message cannot be empty.")
    if len(content) > 12000:
        raise HTTPException(400, "Message is too long.")
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        msg = ProjectMessage(id=str(uuid4()), project_id=project.id, channel="team", role="user", author_email=_norm_email(user.email), content=content)
        db.add(msg)
        project.updated_at = datetime.now(timezone.utc)
        _activity(db, project.id, user.email, "posted in team chat", "message", msg.id)
        db.commit(); db.refresh(msg)
        return {"id":msg.id,"authorEmail":msg.author_email,"content":msg.content,"createdAt":msg.created_at}


@app.delete("/api/projects/{project_id}/messages/{message_id}")
def delete_project_message(project_id: str, message_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, role = _require_project(db, project_id, user.email)
        msg = db.scalar(select(ProjectMessage).where(ProjectMessage.id == message_id, ProjectMessage.project_id == project.id))
        if not msg:
            raise HTTPException(404, "Message not found.")
        if _norm_email(msg.author_email) != _norm_email(user.email) and role not in {"owner", "admin"}:
            raise HTTPException(403, "You cannot delete that message.")
        db.delete(msg)
        _activity(db, project.id, user.email, "deleted a project chat message", "message", message_id)
        db.commit()
        return {"ok": True}


@app.post("/api/projects/{project_id}/messages/{message_id}/note")
def save_project_message_as_note(project_id: str, message_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        msg = db.scalar(select(ProjectMessage).where(ProjectMessage.id == message_id, ProjectMessage.project_id == project.id))
        if not msg:
            raise HTTPException(404, "Message not found.")
        title = (msg.content.splitlines()[0] or "Chat note")[:120]
        note = ProjectNote(id=str(uuid4()), project_id=project.id, title=title, content=msg.content, folder="Chat Notes", pinned=False, source_type="message", source_id=msg.id, created_by_email=_norm_email(user.email))
        db.add(note)
        _activity(db, project.id, user.email, "saved a chat message as a note", "note", note.id, {"sourceMessageId":msg.id})
        db.commit(); db.refresh(note)
        return {"ok": True, "noteId": note.id}


@app.post("/api/projects/{project_id}/beepy")
def project_beepy(project_id: str, body: ProjectBeepyRequest, user: User = Depends(require_user)):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Question is required.")
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        recent_beepy = db.scalars(select(ProjectMessage).where(ProjectMessage.project_id == project.id, ProjectMessage.channel == "beepy").order_by(ProjectMessage.created_at.desc()).limit(10)).all()[::-1]
        notes = db.scalars(select(ProjectNote).where(ProjectNote.project_id == project.id).order_by(ProjectNote.pinned.desc(), ProjectNote.updated_at.desc()).limit(25)).all()
        tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id).order_by(ProjectTask.updated_at.desc()).limit(25)).all()
        ideas = db.scalars(select(ProjectIdea).where(ProjectIdea.project_id == project.id).order_by(ProjectIdea.updated_at.desc()).limit(20)).all()
        decisions = db.scalars(select(ProjectDecision).where(ProjectDecision.project_id == project.id).order_by(ProjectDecision.created_at.desc()).limit(20)).all()
        risks = db.scalars(select(ProjectRisk).where(ProjectRisk.project_id == project.id).order_by(ProjectRisk.updated_at.desc()).limit(20)).all()
        links = db.scalars(select(ProjectLink).where(ProjectLink.project_id == project.id).order_by(ProjectLink.created_at.desc()).limit(20)).all()
        team = db.scalars(select(ProjectMessage).where(ProjectMessage.project_id == project.id, ProjectMessage.channel == "team").order_by(ProjectMessage.created_at.desc()).limit(20)).all()[::-1]
        setting = _workspace_setting(db, project.id, create=False)

        context_parts = [
            f"PROJECT: {project.name}",
            f"DESCRIPTION: {project.description or 'None'}",
            f"STATUS: {setting.status or 'active'}",
            f"CLIENT: {setting.client_name or 'Not set'}",
        ]
        if notes:
            context_parts.append("PROJECT NOTES:\n" + "\n".join(f"- [{x.folder}] {x.title}: {x.content[:700]}" for x in notes))
        if tasks:
            context_parts.append("TASKS:\n" + "\n".join(f"- {x.status}/{x.priority}: {x.title} (assignee {x.assignee_email or 'unassigned'})" for x in tasks))
        if ideas:
            context_parts.append("IDEAS:\n" + "\n".join(f"- {x.status}: {x.title} — {x.description[:400]}" for x in ideas))
        if decisions:
            context_parts.append("DECISIONS:\n" + "\n".join(f"- {x.title}: {x.decision[:500]} Reason: {x.rationale[:300]}" for x in decisions))
        if risks:
            context_parts.append("RISKS:\n" + "\n".join(f"- {x.status} impact={x.impact} likelihood={x.likelihood}: {x.title}. Mitigation: {x.mitigation[:300]}" for x in risks))
        if links:
            context_parts.append("PROJECT LINKS:\n" + "\n".join(f"- {x.title}: {x.url} — {x.description[:250]}" for x in links))
        if team:
            context_parts.append("RECENT TEAM CHAT:\n" + "\n".join(f"- {x.author_email}: {x.content[:500]}" for x in team))
        project_context = "\n\n".join(context_parts)[-14000:]
        history = [{"role":x.role, "content":x.content} for x in recent_beepy]

        user_msg = ProjectMessage(id=str(uuid4()), project_id=project.id, channel="beepy", role="user", author_email=_norm_email(user.email), content=question)
        db.add(user_msg); db.commit()

    prompt = (
        "You are Beepy working inside an MBC MSP project workspace. Use the project context below as working context. "
        "Separate confirmed project facts from recommendations. Challenge risky assumptions, surface dependencies and rollback considerations, and suggest concrete next actions. "
        "Never invent credentials or secrets.\n\n"
        "--- PROJECT CONTEXT ---\n" + project_context + "\n--- END PROJECT CONTEXT ---\n\n"
        "TEAM QUESTION:\n" + question
    )
    try:
        answer = answer_odysseus_tech(prompt, history)
    except OdysseusError as exc:
        raise HTTPException(503, f"Project Beepy is unavailable: {exc}") from exc

    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        assistant_msg = ProjectMessage(id=str(uuid4()), project_id=project.id, channel="beepy", role="assistant", author_email="beepy@mbc.local", content=answer)
        db.add(assistant_msg)
        _activity(db, project.id, user.email, "asked Beepy about the project", "message", assistant_msg.id)
        project.updated_at = datetime.now(timezone.utc)
        db.commit(); db.refresh(assistant_msg)
        return {"response": answer, "messageId": assistant_msg.id, "engine": "odysseus-rag"}


@app.post("/api/projects/{project_id}/notes")
def create_project_note(project_id: str, body: ProjectNoteRequest, user: User = Depends(require_user)):
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Note content is required.")
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        note = ProjectNote(id=str(uuid4()), project_id=project.id, title=(body.title.strip() or "Note")[:300], content=content[:30000], folder=(body.folder.strip() or "General")[:120], pinned=body.pinned, source_type=body.sourceType, source_id=body.sourceId, created_by_email=_norm_email(user.email))
        db.add(note); _activity(db, project.id, user.email, f"created note: {note.title}", "note", note.id); db.commit()
        return {"ok": True, "id": note.id}


@app.patch("/api/projects/{project_id}/notes/{note_id}")
def update_project_note(project_id: str, note_id: str, body: ProjectNoteUpdateRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, role = _require_project(db, project_id, user.email)
        note = db.scalar(select(ProjectNote).where(ProjectNote.id == note_id, ProjectNote.project_id == project.id))
        if not note: raise HTTPException(404, "Note not found.")
        if _norm_email(note.created_by_email) != _norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403, "You cannot edit that note.")
        if body.title is not None: note.title=(body.title.strip() or "Note")[:300]
        if body.content is not None: note.content=body.content.strip()[:30000]
        if body.folder is not None: note.folder=(body.folder.strip() or "General")[:120]
        if body.pinned is not None: note.pinned=body.pinned
        _activity(db, project.id, user.email, f"updated note: {note.title}", "note", note.id); db.commit(); return {"ok": True}


@app.delete("/api/projects/{project_id}/notes/{note_id}")
def delete_project_note(project_id: str, note_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, role = _require_project(db, project_id, user.email)
        note = db.scalar(select(ProjectNote).where(ProjectNote.id == note_id, ProjectNote.project_id == project.id))
        if not note: raise HTTPException(404, "Note not found.")
        if _norm_email(note.created_by_email) != _norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403, "You cannot delete that note.")
        db.delete(note); _activity(db, project.id, user.email, "deleted a project note", "note", note_id); db.commit(); return {"ok": True}


@app.post("/api/projects/{project_id}/links")
def create_project_link(project_id: str, body: ProjectLinkRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, _ = _require_project(db, project_id, user.email)
        link = ProjectLink(id=str(uuid4()), project_id=project.id, title=(body.title.strip() or "Link")[:300], url=_valid_http_url(body.url), description=body.description.strip()[:4000], created_by_email=_norm_email(user.email))
        db.add(link); _activity(db, project.id, user.email, f"added link: {link.title}", "link", link.id); db.commit(); return {"ok":True,"id":link.id}


@app.delete("/api/projects/{project_id}/links/{link_id}")
def delete_project_link(project_id: str, link_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project, role = _require_project(db, project_id, user.email)
        link = db.scalar(select(ProjectLink).where(ProjectLink.id == link_id, ProjectLink.project_id == project.id))
        if not link: raise HTTPException(404, "Link not found.")
        if _norm_email(link.created_by_email) != _norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403, "You cannot delete that link.")
        db.delete(link); _activity(db, project.id, user.email, "removed a project link", "link", link_id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/tasks")
def create_project_task(project_id: str, body: ProjectTaskRequest, user: User = Depends(require_user)):
    title=body.title.strip()
    if not title: raise HTTPException(400,"Task title is required.")
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        task=ProjectTask(id=str(uuid4()),project_id=project.id,title=title[:400],description=body.description.strip()[:10000],status=body.status.strip().lower()[:40],priority=body.priority.strip().lower()[:30],assignee_email=_norm_email(body.assigneeEmail) or None,due_at=body.dueAt,created_by_email=_norm_email(user.email))
        db.add(task); _activity(db,project.id,user.email,f"created task: {task.title}","task",task.id); db.commit(); return {"ok":True,"id":task.id}


@app.patch("/api/projects/{project_id}/tasks/{task_id}")
def update_project_task(project_id: str, task_id: str, body: ProjectTaskUpdateRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        task=db.scalar(select(ProjectTask).where(ProjectTask.id==task_id,ProjectTask.project_id==project.id))
        if not task: raise HTTPException(404,"Task not found.")
        if body.title is not None: task.title=(body.title.strip() or task.title)[:400]
        if body.description is not None: task.description=body.description.strip()[:10000]
        if body.status is not None: task.status=body.status.strip().lower()[:40]
        if body.priority is not None: task.priority=body.priority.strip().lower()[:30]
        if body.assigneeEmail is not None: task.assignee_email=_norm_email(body.assigneeEmail) or None
        if body.dueAt is not None: task.due_at=body.dueAt
        _activity(db,project.id,user.email,f"updated task: {task.title}","task",task.id); db.commit(); return {"ok":True}


@app.delete("/api/projects/{project_id}/tasks/{task_id}")
def delete_project_task(project_id: str, task_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,role=_require_project(db,project_id,user.email)
        task=db.scalar(select(ProjectTask).where(ProjectTask.id==task_id,ProjectTask.project_id==project.id))
        if not task: raise HTTPException(404,"Task not found.")
        if _norm_email(task.created_by_email)!=_norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403,"You cannot delete that task.")
        db.delete(task); _activity(db,project.id,user.email,"deleted a project task","task",task_id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/ideas")
def create_project_idea(project_id: str, body: ProjectIdeaRequest, user: User = Depends(require_user)):
    title=body.title.strip()
    if not title: raise HTTPException(400,"Idea title is required.")
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        idea=ProjectIdea(id=str(uuid4()),project_id=project.id,title=title[:400],description=body.description.strip()[:12000],status=body.status.strip().lower()[:40],created_by_email=_norm_email(user.email))
        db.add(idea); _activity(db,project.id,user.email,f"proposed idea: {idea.title}","idea",idea.id); db.commit(); return {"ok":True,"id":idea.id}


@app.patch("/api/projects/{project_id}/ideas/{idea_id}")
def update_project_idea(project_id: str, idea_id: str, body: ProjectIdeaUpdateRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,role=_require_project(db,project_id,user.email)
        idea=db.scalar(select(ProjectIdea).where(ProjectIdea.id==idea_id,ProjectIdea.project_id==project.id))
        if not idea: raise HTTPException(404,"Idea not found.")
        if _norm_email(idea.created_by_email)!=_norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403,"You cannot edit that idea.")
        if body.title is not None: idea.title=(body.title.strip() or idea.title)[:400]
        if body.description is not None: idea.description=body.description.strip()[:12000]
        if body.status is not None:
            if role not in {"owner","admin"} and body.status not in {"discussing"}: raise HTTPException(403,"Owners/Admins approve or reject ideas.")
            idea.status=body.status.strip().lower()[:40]
        _activity(db,project.id,user.email,f"updated idea: {idea.title}","idea",idea.id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/ideas/{idea_id}/vote")
def vote_project_idea(project_id: str, idea_id: str, body: ProjectIdeaVoteRequest, user: User = Depends(require_user)):
    vote=1 if body.vote>0 else (-1 if body.vote<0 else 0)
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        idea=db.scalar(select(ProjectIdea).where(ProjectIdea.id==idea_id,ProjectIdea.project_id==project.id))
        if not idea: raise HTTPException(404,"Idea not found.")
        existing=db.scalar(select(ProjectIdeaVote).where(ProjectIdeaVote.idea_id==idea.id,func.lower(ProjectIdeaVote.email)==_norm_email(user.email)))
        if vote==0:
            if existing: db.delete(existing)
        elif existing: existing.vote=vote
        else: db.add(ProjectIdeaVote(id=str(uuid4()),idea_id=idea.id,email=_norm_email(user.email),vote=vote))
        db.commit(); return {"ok":True}


@app.delete("/api/projects/{project_id}/ideas/{idea_id}")
def delete_project_idea(project_id: str, idea_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,role=_require_project(db,project_id,user.email)
        idea=db.scalar(select(ProjectIdea).where(ProjectIdea.id==idea_id,ProjectIdea.project_id==project.id))
        if not idea: raise HTTPException(404,"Idea not found.")
        if _norm_email(idea.created_by_email)!=_norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403,"You cannot delete that idea.")
        db.delete(idea); _activity(db,project.id,user.email,"deleted a project idea","idea",idea_id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/decisions")
def create_project_decision(project_id: str, body: ProjectDecisionRequest, user: User = Depends(require_user)):
    if not body.title.strip() or not body.decision.strip(): raise HTTPException(400,"Decision title and decision are required.")
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email,{"owner","admin"})
        item=ProjectDecision(id=str(uuid4()),project_id=project.id,title=body.title.strip()[:400],decision=body.decision.strip()[:16000],rationale=body.rationale.strip()[:12000],created_by_email=_norm_email(user.email))
        db.add(item); _activity(db,project.id,user.email,f"recorded decision: {item.title}","decision",item.id); db.commit(); return {"ok":True,"id":item.id}


@app.delete("/api/projects/{project_id}/decisions/{decision_id}")
def delete_project_decision(project_id: str, decision_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email,{"owner","admin"})
        item=db.scalar(select(ProjectDecision).where(ProjectDecision.id==decision_id,ProjectDecision.project_id==project.id))
        if not item: raise HTTPException(404,"Decision not found.")
        db.delete(item); _activity(db,project.id,user.email,"deleted a project decision","decision",decision_id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/risks")
def create_project_risk(project_id: str, body: ProjectRiskRequest, user: User = Depends(require_user)):
    if not body.title.strip(): raise HTTPException(400,"Risk title is required.")
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        risk=ProjectRisk(id=str(uuid4()),project_id=project.id,title=body.title.strip()[:400],impact=body.impact.strip().lower()[:30],likelihood=body.likelihood.strip().lower()[:30],mitigation=body.mitigation.strip()[:12000],status=body.status.strip().lower()[:40],created_by_email=_norm_email(user.email))
        db.add(risk); _activity(db,project.id,user.email,f"added risk: {risk.title}","risk",risk.id); db.commit(); return {"ok":True,"id":risk.id}


@app.patch("/api/projects/{project_id}/risks/{risk_id}")
def update_project_risk(project_id: str, risk_id: str, body: ProjectRiskUpdateRequest, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        risk=db.scalar(select(ProjectRisk).where(ProjectRisk.id==risk_id,ProjectRisk.project_id==project.id))
        if not risk: raise HTTPException(404,"Risk not found.")
        if body.title is not None: risk.title=(body.title.strip() or risk.title)[:400]
        if body.impact is not None: risk.impact=body.impact.strip().lower()[:30]
        if body.likelihood is not None: risk.likelihood=body.likelihood.strip().lower()[:30]
        if body.mitigation is not None: risk.mitigation=body.mitigation.strip()[:12000]
        if body.status is not None: risk.status=body.status.strip().lower()[:40]
        _activity(db,project.id,user.email,f"updated risk: {risk.title}","risk",risk.id); db.commit(); return {"ok":True}


@app.delete("/api/projects/{project_id}/risks/{risk_id}")
def delete_project_risk(project_id: str, risk_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,role=_require_project(db,project_id,user.email)
        risk=db.scalar(select(ProjectRisk).where(ProjectRisk.id==risk_id,ProjectRisk.project_id==project.id))
        if not risk: raise HTTPException(404,"Risk not found.")
        if _norm_email(risk.created_by_email)!=_norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403,"You cannot delete that risk.")
        db.delete(risk); _activity(db,project.id,user.email,"deleted a project risk","risk",risk_id); db.commit(); return {"ok":True}


@app.post("/api/projects/{project_id}/files")
async def upload_project_file(project_id: str, file: UploadFile = File(...), user: User = Depends(require_user)):
    original = Path(file.filename or "upload.bin").name
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", original).strip()[:240] or "upload.bin"
    content = await file.read(PROJECT_MAX_UPLOAD_BYTES + 1)
    if len(content) > PROJECT_MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Project files are limited to 25 MB each.")
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        rel = f"{project.id}/{uuid4().hex}_{safe}"
        path = PROJECT_UPLOAD_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        row=ProjectFile(id=str(uuid4()),project_id=project.id,filename=safe,stored_name=rel,content_type=(file.content_type or "application/octet-stream")[:200],size_bytes=len(content),uploaded_by_email=_norm_email(user.email))
        db.add(row); _activity(db,project.id,user.email,f"uploaded file: {safe}","file",row.id,{"sizeBytes":len(content)}); db.commit(); return {"ok":True,"id":row.id}


@app.get("/api/projects/{project_id}/files/{file_id}/download")
def download_project_file(project_id: str, file_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,_=_require_project(db,project_id,user.email)
        row=db.scalar(select(ProjectFile).where(ProjectFile.id==file_id,ProjectFile.project_id==project.id))
        if not row: raise HTTPException(404,"File not found.")
        path=(PROJECT_UPLOAD_ROOT / row.stored_name).resolve()
        root=PROJECT_UPLOAD_ROOT.resolve()
        if root not in path.parents or not path.exists(): raise HTTPException(404,"Stored project file is missing.")
        return FileResponse(path, media_type=row.content_type, filename=row.filename)


@app.delete("/api/projects/{project_id}/files/{file_id}")
def delete_project_file(project_id: str, file_id: str, user: User = Depends(require_user)):
    with SessionLocal() as db:
        project,role=_require_project(db,project_id,user.email)
        row=db.scalar(select(ProjectFile).where(ProjectFile.id==file_id,ProjectFile.project_id==project.id))
        if not row: raise HTTPException(404,"File not found.")
        if _norm_email(row.uploaded_by_email)!=_norm_email(user.email) and role not in {"owner","admin"}: raise HTTPException(403,"You cannot delete that file.")
        path=PROJECT_UPLOAD_ROOT / row.stored_name
        try: path.unlink(missing_ok=True)
        except OSError: pass
        db.delete(row); _activity(db,project.id,user.email,"deleted a project file","file",file_id,{"filename":row.filename}); db.commit(); return {"ok":True}



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
    """Resolve Auto before retrieval. Each source stays isolated."""
    requested = (requested_mode or "auto").strip().lower()
    if requested == "smart":
        requested = "auto"
    if requested in {"tickets", "tech", "email"}:
        return requested, {"requested": requested, "reason": "manual"}

    interpretation = interpret_question(question=question, user_email=user_email)
    lower = question.lower()

    ticket_markers = (
        "ticket", "tickets", "autotask", "ticket number", "resolution note",
        "assigned ticket", "closed ticket", "service ticket",
    )
    email_markers = (
        "email", "emails", "emailed", "e-mail", "mailbox", "inbox",
        "sent items", "subject line", "mail message", "correspondence",
        "message from", "message to",
    )
    retrieval_leads = (
        "find", "search", "show", "look up", "look for", "what did",
        "when did", "who emailed", "who sent", "summarize the", "summary of",
    )
    tech_leads = (
        "how do i", "how can i", "how to", "why does", "why is",
        "what causes", "troubleshoot", "not working", "cannot connect",
        "can't connect", "error", "crashing", "freezing", "won't open",
        "will not open",
    )

    # Explicit source names always win. This is the strongest protection against
    # Email results being confused with Autotask tickets.
    if any(term in lower for term in ticket_markers):
        return "tickets", {
            "requested": "auto", "reason": "explicit ticket wording",
            "interpretation": interpretation.to_dict(),
        }
    if any(term in lower for term in email_markers):
        # "my email is not working" is troubleshooting, not historical mail search.
        if any(term in lower for term in tech_leads) and not any(term in lower for term in retrieval_leads):
            return "tech", {
                "requested": "auto", "reason": "technical wording despite email noun",
                "interpretation": interpretation.to_dict(),
            }
        return "email", {
            "requested": "auto", "reason": "explicit email wording",
            "interpretation": interpretation.to_dict(),
        }

    if any(term in lower for term in tech_leads):
        return "tech", {
            "requested": "auto", "reason": "technical wording",
            "interpretation": interpretation.to_dict(),
        }

    # Communication language without a named source is intentionally NOT guessed.
    # Beepy asks the user whether they mean Email or Tickets and searches neither.
    ambiguous_communication = (
        "what was said", "tell them", "told them", "told the client",
        "discussed with", "talked with", "talked to", "communicated with",
        "what did we promise", "what did we tell",
    )
    ambiguous_pattern = re.search(
        r"\bwhat did\b.{0,80}\b(?:tell|say|discuss|promise|communicat|send)\w*\b",
        lower,
    )
    if ambiguous_pattern or any(term in lower for term in ambiguous_communication):
        return "clarify", {
            "requested": "auto", "reason": "ambiguous communication source",
            "interpretation": interpretation.to_dict(),
        }

    if interpretation.intent == "ticket_search":
        return "tickets", {
            "requested": "auto", "reason": "ticket-history wording",
            "interpretation": interpretation.to_dict(),
        }

    historical = any(term in lower for term in (
        "worked", "done", "completed", "closed", "resolved", "created",
        "assigned", "history", "previous", "past", "recent", "has had",
        "have had", "did yesterday", "did today",
    ))
    if historical and (
        interpretation.technician or interpretation.company or interpretation.date_from
    ):
        return "tickets", {
            "requested": "auto", "reason": "historical entity/date request",
            "interpretation": interpretation.to_dict(),
        }

    return "tech", {
        "requested": "auto", "reason": "default technical question",
        "interpretation": interpretation.to_dict(),
    }


def _ticket_sources(tickets: list[Ticket]) -> list[dict]:
    return [
        {
            "sourceType": "ticket",
            "ticketNumber": t.ticket_number,
            "title": t.title,
            "company": t.company_name,
            "createdDate": str(t.create_date or ""),
            "url": autotask_client.ticket_web_url(t.id),
        }
        for t in tickets
    ]


def _email_sources(hits) -> list[dict]:
    rows = []
    for index, hit in enumerate(hits, 1):
        when = hit.sent_at or hit.received_at
        rows.append({
            "sourceType": "email",
            "emailIndex": index,
            "emailId": hit.id,
            "title": hit.subject,
            "subject": hit.subject,
            "sender": hit.sender_address,
            "senderName": hit.sender_name,
            "sentDate": when.isoformat() if when else "",
            "mailbox": hit.mailbox,
            "folder": hit.folder,
            "hasAttachments": hit.has_attachments,
            "url": hit.web_link or "",
        })
    return rows


def _clarify_source_answer() -> str:
    return (
        "## Which source should I search?\n\n"
        "That request could refer to **Microsoft 365 email** or **Autotask tickets**. "
        "I have not searched either source yet so I do not mix them together.\n\n"
        "Choose **Email Intelligence** or **Tickets** from the mode selector and send the question again."
    )


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
                id=str(uuid4()), user_email=user.email, title=question[:70],
            )
            db.add(conversation)
            db.flush()

        history_rows = db.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
        ).all()
        history = [{"role": x.role, "content": x.content} for x in history_rows[-12:]]
        db.add(Message(
            conversation_id=conversation.id, user_email=user.email,
            role="user", content=question, sources=[],
        ))
        db.commit()
        conversation_id = conversation.id

    requested_mode = body.mode.lower()
    mode, route_plan = _resolve_chat_mode(requested_mode, question, user.email)
    tickets = []
    email_hits = []

    if mode == "tickets":
        t0 = time.perf_counter()
        tickets = search_tickets(question)
        print(f"Ticket search took {(time.perf_counter() - t0):.3f} seconds", flush=True)
        answer = answer_ticket_question(question, tickets, history)
        engine = "autotask-hybrid" if tickets else "autotask-no-match"
        sources = _ticket_sources(tickets)
    elif mode == "email":
        if not can_search_tenant_email(user.email):
            answer = (
                "## Email Intelligence access required\n\n"
                "Your Beepy account is not permitted to search tenant-wide Microsoft 365 email. "
                "No tickets, Tech RAG, or other source was searched as a fallback."
            )
            engine = "email-denied"
            sources = []
        else:
            t0 = time.perf_counter()
            email_hits = search_emails(question)
            search_ms = int((time.perf_counter() - t0) * 1000)
            audit_email_search(user.email, question, email_hits, search_ms)
            answer = answer_email_question(question, email_hits, history)
            engine = "m365-email" if email_hits else "m365-email-no-match"
            sources = _email_sources(email_hits)
    elif mode == "clarify":
        answer = _clarify_source_answer()
        engine = "source-clarification"
        sources = []
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
                    "was also unavailable. Ticket Search and Email Intelligence remain isolated."
                )
                engine = "tech-unavailable"
        sources = []

    with SessionLocal() as db:
        db.add(Message(
            conversation_id=conversation_id, user_email=user.email,
            role="assistant", content=answer, sources=sources,
        ))
        db.commit()

    return {
        "answer": answer,
        "conversationId": conversation_id,
        "sources": sources,
        "engine": engine,
        "matchedTickets": len(tickets),
        "matchedEmails": len(email_hits),
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
                id=str(uuid4()), user_email=user.email, title=question[:70],
            )
            db.add(conversation)
            db.flush()

        history_rows = db.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
        ).all()
        history = [{"role": row.role, "content": row.content} for row in history_rows[-12:]]
        db.add(Message(
            conversation_id=conversation.id, user_email=user.email,
            role="user", content=question, sources=[],
        ))
        db.commit()
        conversation_id = conversation.id

    requested_mode = body.mode.lower()
    mode, route_plan = _resolve_chat_mode(requested_mode, question, user.email)
    started = time.perf_counter()
    tickets = search_tickets(question) if mode == "tickets" else []
    email_hits = search_emails(question) if mode == "email" and can_search_tenant_email(user.email) else []
    sources = _ticket_sources(tickets) if mode == "tickets" else _email_sources(email_hits) if mode == "email" else []

    def event_stream():
        if mode == "tickets":
            engine = "autotask-exact" if tickets else "autotask-no-match"
            answer = answer_ticket_question(question, tickets, history)
        elif mode == "email":
            if not can_search_tenant_email(user.email):
                answer = (
                    "## Email Intelligence access required\n\n"
                    "Your Beepy account is not permitted to search tenant-wide Microsoft 365 email. "
                    "No tickets or Tech RAG were searched as a fallback."
                )
                engine = "email-denied"
            else:
                answer = answer_email_question(question, email_hits, history)
                engine = "m365-email" if email_hits else "m365-email-no-match"
        elif mode == "clarify":
            answer = _clarify_source_answer()
            engine = "source-clarification"
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
                        "was also unavailable. Ticket Search and Email Intelligence remain isolated."
                    )
                    engine = "tech-unavailable"

        yield f"event: meta\ndata: {json.dumps({
            'conversationId': conversation_id,
            'sources': sources,
            'engine': engine,
            'matchedTickets': len(tickets),
            'matchedEmails': len(email_hits),
            'resolvedMode': mode,
        })}\n\n"
        yield f"event: token\ndata: {json.dumps({'text': answer})}\n\n"

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if mode == "email" and can_search_tenant_email(user.email):
            audit_email_search(user.email, question, email_hits, elapsed_ms)
        with SessionLocal() as db:
            db.add(Message(
                conversation_id=conversation_id, user_email=user.email,
                role="assistant", content=answer, sources=sources,
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
                    "matchedEmailCount": len(email_hits),
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
    # Beepy changes frequently during development. Prevent browsers/proxies from
    # pinning old JS/CSS so a rebuild actually shows the new UI immediately.
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if path and requested.is_file():
        return FileResponse(requested, headers=headers)
    return FileResponse(static_dir / "index.html", headers=headers)
