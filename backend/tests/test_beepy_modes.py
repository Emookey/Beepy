"""Focused behavioral checks for local modes and the Kal cutover helper."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import sys
import types
from types import SimpleNamespace

import pytest
from sqlalchemy.types import UserDefinedType

from app.config import get_settings
from app.kal import KalAuthenticationError


def _load_main(monkeypatch):
    """Load Beepy without installing the unavailable pgvector test extra."""

    class Vector(UserDefinedType):
        cache_ok = True

        def __init__(self, dimensions=None):
            self.dimensions = dimensions

        def get_col_spec(self, **_kwargs):
            return "VECTOR"

    package = types.ModuleType("pgvector")
    module = types.ModuleType("pgvector.sqlalchemy")
    module.Vector = Vector
    package.sqlalchemy = module
    monkeypatch.setitem(sys.modules, "pgvector", package)
    monkeypatch.setitem(sys.modules, "pgvector.sqlalchemy", module)

    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "ENTRA_CLIENT_ID": "placeholder",
        "ENTRA_TENANT_ID": "placeholder",
        "ALLOWED_EMAIL_DOMAIN": "example.invalid",
        "AUTOTASK_USERNAME": "placeholder",
        "AUTOTASK_SECRET": "placeholder",
        "AUTOTASK_INTEGRATION_CODE": "placeholder",
        "AUTOTASK_BASE_URL": "https://example.invalid/autotask",
        "KAL_BASE_URL": "http://kal.example.invalid",
        "KAL_SERVICE_TOKEN_PATH": "/nonexistent/test-only-token",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    return importlib.import_module("app.main")


@pytest.fixture
def main_module(monkeypatch):
    return _load_main(monkeypatch)


def test_manual_ticket_and_email_modes_remain_compatible(main_module):
    assert main_module._resolve_chat_mode("tickets", "question", "user@example.invalid")[0] == "tickets"
    assert main_module._resolve_chat_mode("email", "question", "user@example.invalid")[0] == "email"


def test_legacy_odysseus_rag_mode_resolves_to_technical_kal(main_module):
    mode, plan = main_module._resolve_chat_mode(
        "odysseus-rag", "technical question", "user@example.invalid"
    )
    assert mode == "tech"
    assert plan["reason"] == "Kal technical compatibility alias"


def test_persisted_history_is_partitioned_by_mode(main_module):
    rows = [
        SimpleNamespace(role="user", content="technical turn", sources=[main_module._mode_marker("tech")]),
        SimpleNamespace(role="assistant", content="ticket answer", sources=[main_module._mode_marker("tickets")]),
        SimpleNamespace(role="user", content="project or email evidence", sources=[main_module._mode_marker("email")]),
        SimpleNamespace(role="assistant", content="untagged historical turn", sources=[]),
    ]
    assert main_module._history_for_mode(rows, "tech") == [
        {"role": "user", "content": "technical turn"}
    ]
    assert main_module._history_for_mode(rows, "tickets") == [
        {"role": "assistant", "content": "ticket answer"}
    ]
    assert "project or email evidence" not in str(main_module._history_for_mode(rows, "tech"))


def test_technical_helper_uses_hashed_actor_and_returns_grounding(main_module, monkeypatch):
    from app.kal import KalSource, KalTechnicalResponse

    captured = {}

    def fake_answer(message, **kwargs):
        captured.update(message=message, kwargs=kwargs)
        return KalTechnicalResponse(
            "answer", True, "grounded", (KalSource("guide.pdf", "structured_exact"),), "req-1"
        )

    monkeypatch.setattr(main_module, "answer_kal_technical", fake_answer)
    result = main_module._technical_answer("current technical turn", "person@example.invalid")
    assert captured["message"] == "current technical turn"
    assert "history" not in captured["kwargs"]
    assert "person@example.invalid" not in str(captured["kwargs"])
    assert captured["kwargs"]["actor_identifier"].startswith("beepy-user:")
    assert result == (
        "answer",
        "kal-shared-knowledge",
        [{"sourceType": "kal-shared-knowledge", "title": "guide.pdf", "grounding": "structured_exact"}],
        "grounded",
        "req-1",
    )


def test_technical_error_logs_only_classification(main_module, monkeypatch, caplog):
    sensitive_dummy = "ody_" + "Z" * 43

    def reject(*_args, **_kwargs):
        raise KalAuthenticationError(sensitive_dummy)

    monkeypatch.setattr(main_module, "answer_kal_technical", reject)
    result = main_module._technical_answer("question", "person@example.invalid")
    assert result[1] == "kal-technical-unavailable"
    assert sensitive_dummy not in caplog.text
    assert sensitive_dummy not in str(result)
    assert "direct model fallback" in result[0]


def test_email_answers_remain_local_without_using_cross_mode_history(main_module):
    from app.email_search import EmailHit, answer_email_question

    hit = EmailHit(
        id="email-1",
        internet_message_id=None,
        subject="Example subject",
        sender_name="Example Sender",
        sender_address="sender@example.invalid",
        to_recipients=[],
        cc_recipients=[],
        sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        received_at=None,
        body_text="Local indexed body",
        body_preview="Local preview",
        has_attachments=False,
        web_link="",
        mailbox="example",
        folder="example",
        score=1.0,
    )
    answer = answer_email_question(
        "Find the message",
        [hit],
        history=[{"role": "user", "content": "private ticket/project context"}],
    )
    assert "Kal was not contacted" in answer
    assert "Local preview" in answer
    assert "private ticket/project context" not in answer
