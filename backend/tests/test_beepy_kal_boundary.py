"""Static boundary tests for Beepy's Phase 3E.6B route cutover."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
EMAIL = (ROOT / "backend/app/email_search.py").read_text(encoding="utf-8")
KAL = (ROOT / "backend/app/kal.py").read_text(encoding="utf-8")
LEGACY = (ROOT / "backend/app/odysseus.py").read_text(encoding="utf-8")
UI = (ROOT / "backend/static/app.js").read_text(encoding="utf-8")


def _function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _segment(source: str, name: str) -> str:
    return ast.get_source_segment(source, _function(source, name)) or ""


def test_canonical_main_has_no_legacy_or_local_technical_fallback_import():
    imports = "\n".join(
        line for line in MAIN.splitlines() if line.startswith("from ") or line.startswith("import ")
    )
    assert ".odysseus" not in imports
    assert "answer_tech_question" not in imports
    assert "chat_stream" not in imports
    technical = _segment(MAIN, "_technical_answer")
    assert "answer_kal_technical" in technical
    assert "answer_tech_question" not in technical
    assert "ollama" not in technical.lower()


def test_technical_client_surface_has_only_dedicated_kal_routes():
    assert '"/api/integrations/beepy/capabilities"' in KAL
    assert '"/api/integrations/beepy/technical-chat"' in KAL
    for forbidden in (
        "/api/chat", "/api/session", "/api/sessions", "/api/memory",
        "/api/documents", "/api/tools", "/api/agents", "/api/webhooks",
    ):
        assert forbidden not in KAL


def test_health_and_compatibility_probe_create_no_session():
    probe = _segment(LEGACY, "probe_odysseus")
    assert "probe_kal_capability" in probe
    assert "/api/sessions" not in probe
    assert "post(" not in probe.lower()
    capability = _segment(MAIN, "kal_capabilities")
    assert "probe_kal_capability" in capability


def test_technical_request_excludes_all_persisted_history_and_business_context():
    client = _segment(KAL, "answer_kal_technical")
    assert 'json={"message": message}' in client
    for forbidden in ("history", "ticket", "project", "email", "attachment"):
        assert forbidden not in client.lower()
    helper = _segment(MAIN, "_technical_answer")
    assert "history" not in helper.lower()
    assert "_history_for_mode(history_rows, mode)" in MAIN


def test_ticket_mode_remains_local_and_does_not_enter_kal_request():
    chat = _segment(MAIN, "chat_endpoint")
    assert 'mode == "tickets"' in chat
    assert "answer_ticket_question(question, tickets, history)" in chat
    assert "answer_kal_technical" not in chat
    assert "ticket" not in _segment(KAL, "answer_kal_technical").lower()


def test_project_evidence_is_not_built_or_sent_to_kal():
    project = _segment(MAIN, "project_beepy")
    assert "_require_project" in project
    assert "answer_kal_technical" not in project
    assert "answer_odysseus" not in project
    for forbidden in ("ProjectNote", "ProjectTask", "ProjectRisk", "project_context", "TEAM CHAT"):
        assert forbidden not in project
    assert "business-data contract" in project


def test_email_evidence_remains_local_and_never_calls_kal_or_legacy_session():
    answer = _segment(EMAIL, "answer_email_question")
    assert "answer_kal" not in answer
    assert "answer_odysseus" not in answer
    assert "Kal was not contacted" in answer
    assert "from .odysseus" not in EMAIL


def test_human_actor_is_audit_only_and_never_selects_token():
    actor = _segment(KAL, "beepy_actor_audit_id")
    reader = _segment(KAL, "_read_service_token")
    client = _segment(KAL, "answer_kal_technical")
    assert "sha256" in actor
    assert "user_email" not in reader
    assert "actor_identifier" not in reader
    assert "ACTOR_HEADER" in client
    assert '"owner"' not in client and '"scopes"' not in client


def test_legacy_mode_alias_maps_only_to_safe_technical_mode():
    resolver = _segment(MAIN, "_resolve_chat_mode")
    assert '"odysseus-rag"' in resolver
    assert 'return "tech"' in resolver
    assert "answer_odysseus" not in resolver


def test_provider_and_model_selection_are_absent_from_kal_request():
    client = _segment(KAL, "answer_kal_technical")
    request_call = client[client.index("_request("):]
    for forbidden in ("model", "provider", "endpoint_url", "ollama", "temperature"):
        assert forbidden not in request_call.lower()


def test_current_facing_ui_uses_kal_and_bounds_source_rendering():
    assert "Kal · shared technical knowledge" in UI
    assert "Kal technical support" in UI
    assert "Odysseus RAG ·" not in UI
    assert "Local Qwen fallback" not in UI
    assert "kal-shared-knowledge" in UI
    assert "groundingStatus" in UI


def test_unrelated_business_modes_remain_available():
    assert 'mode == "tickets"' in MAIN
    assert 'mode == "email"' in MAIN
    assert "search_tickets(question)" in MAIN
    assert "search_emails(question)" in MAIN
    assert "answer_ticket_question" in MAIN
    assert "answer_email_question" in MAIN
