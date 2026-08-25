"""Focused tests for the scoped, provider-neutral Kal client."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import kal
from app.config import Settings


DUMMY_TOKEN = "ody_" + "A" * 43


def _settings(token_path: Path, **overrides):
    values = {
        "kal_base_url": "http://kal.example.invalid",
        "kal_service_token_path": str(token_path),
        "kal_timeout_seconds": 30,
        "odysseus_base_url": None,
        "odysseus_token_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _token_file(tmp_path: Path, value: str = DUMMY_TOKEN) -> Path:
    path = tmp_path / "kal-service-token"
    path.write_text(value, encoding="utf-8")
    return path


def _required_environment(monkeypatch):
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "ENTRA_CLIENT_ID": "placeholder",
        "ENTRA_TENANT_ID": "placeholder",
        "ALLOWED_EMAIL_DOMAIN": "example.invalid",
        "AUTOTASK_USERNAME": "placeholder",
        "AUTOTASK_SECRET": "placeholder",
        "AUTOTASK_INTEGRATION_CODE": "placeholder",
        "AUTOTASK_BASE_URL": "https://example.invalid/autotask",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _response(status="grounded", *, sources=None, used=None):
    if sources is None:
        sources = ([{"label": "technical-guide.pdf", "grounding": "structured_exact"}]
                   if status == "grounded" else [])
    if used is None:
        used = status == "grounded"
    return {
        "contract_version": "beepy-kal.v1",
        "response": "bounded technical answer",
        "shared_knowledge_used": used,
        "grounding_status": status,
        "sources": sources,
        "request_id": "req-123",
    }


def test_canonical_single_service_credential_and_exact_request(tmp_path):
    token_path = _token_file(tmp_path)
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json=_response())

    result = kal.answer_kal_technical(
        "How do I diagnose this technical issue?",
        actor_identifier=kal.beepy_actor_audit_id("human@example.invalid"),
        request_id="req-123",
        settings=_settings(token_path),
        transport=httpx.MockTransport(handler),
    )

    request = captured["request"]
    assert request.method == "POST"
    assert request.url.path == "/api/integrations/beepy/technical-chat"
    assert request.headers["authorization"] == f"Bearer {DUMMY_TOKEN}"
    assert request.headers["x-kal-request-id"] == "req-123"
    assert request.headers["x-kal-business-operation"] == "technical-chat"
    actor = request.headers["x-kal-beepy-actor"]
    assert actor.startswith("beepy-user:")
    assert "human" not in actor and "example" not in actor
    assert request.read() == b'{"message":"How do I diagnose this technical issue?"}'
    assert result.grounding_status == "grounded"
    assert result.shared_knowledge_used is True


def test_human_identity_cannot_select_authorization_or_provider(tmp_path):
    token_path = _token_file(tmp_path)

    def handler(request: httpx.Request):
        body = request.read().decode("utf-8")
        assert "human@example.invalid" not in body
        for forbidden in (
            "owner", "user_email", "session", "history", "provider", "model",
            "endpoint", "ollama", "tools", "attachments", "documents", "web",
        ):
            assert forbidden not in body.lower()
        assert request.headers["authorization"] == f"Bearer {DUMMY_TOKEN}"
        return httpx.Response(200, json=_response("no_match"))

    kal.answer_kal_technical(
        "Current technical question only",
        actor_identifier=kal.beepy_actor_audit_id("human@example.invalid"),
        settings=_settings(token_path),
        transport=httpx.MockTransport(handler),
    )


def test_capability_probe_uses_only_dedicated_get_and_no_prompt(tmp_path):
    token_path = _token_file(tmp_path)

    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.path == "/api/integrations/beepy/capabilities"
        assert request.read() == b""
        assert "session" not in request.url.path
        return httpx.Response(200, json={
            "contract_version": "beepy-kal.v1",
            "capability": "technical_shared_knowledge",
            "stateless": True,
            "grounding_states": ["grounded", "no_match", "degraded"],
        })

    result = kal.probe_kal_capability(
        settings=_settings(token_path), transport=httpx.MockTransport(handler)
    )
    assert result.stateless is True
    assert result.grounding_states == ("grounded", "no_match", "degraded")


@pytest.mark.parametrize("status", ["grounded", "no_match", "degraded"])
def test_exact_grounding_states_are_consumed(tmp_path, status):
    token_path = _token_file(tmp_path)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_response(status))
    )
    result = kal.answer_kal_technical(
        "Question", actor_identifier="beepy-user:123", settings=_settings(token_path),
        transport=transport,
    )
    assert result.grounding_status == status
    assert result.shared_knowledge_used is (status == "grounded")
    assert bool(result.sources) is (status == "grounded")


@pytest.mark.parametrize(
    "payload",
    [
        _response("grounded", sources=[{"label": "/private/path", "grounding": "semantic"}]),
        _response("grounded", sources=[{"label": "x" * 121, "grounding": "semantic"}]),
        _response("grounded", sources=[{"label": "safe", "grounding": "x" * 41}]),
        _response("grounded", sources=[{"label": "safe", "grounding": "semantic", "owner": "private"}]),
        _response("grounded", sources=[{"label": "safe", "grounding": "semantic"}] * 6),
        _response("no_match", sources=[{"label": "safe", "grounding": "semantic"}]),
        _response("grounded", used=False),
    ],
)
def test_unsafe_or_inconsistent_source_metadata_fails_closed(tmp_path, payload):
    token_path = _token_file(tmp_path)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(kal.KalContractError):
        kal.answer_kal_technical(
            "Question", actor_identifier="beepy-user:123", settings=_settings(token_path),
            transport=transport,
        )


def test_legacy_token_map_and_first_token_fallback_are_rejected(tmp_path):
    token_path = _token_file(
        tmp_path,
        json.dumps({"first": "ody_" + "A" * 43, "second": "ody_" + "B" * 43}),
    )
    with pytest.raises(kal.KalConfigurationError, match="per-user token maps"):
        kal.probe_kal_capability(settings=_settings(token_path))


@pytest.mark.parametrize("value", ["", "not-a-token", "ody_short", "Bearer ody_" + "A" * 43])
def test_missing_or_malformed_service_credential_fails_closed(tmp_path, value):
    path = tmp_path / "credential"
    if value != "":
        path.write_text(value, encoding="utf-8")
    with pytest.raises(kal.KalConfigurationError) as caught:
        kal.probe_kal_capability(settings=_settings(path))
    assert DUMMY_TOKEN not in str(caught.value)


def test_kal_only_configuration_works(tmp_path):
    token_path = _token_file(tmp_path)
    config = kal.resolve_kal_config(_settings(token_path))
    assert config.base_url == "http://kal.example.invalid"
    assert config.service_token_path == token_path


def test_canonical_environment_names_reach_settings(monkeypatch, tmp_path):
    token_path = _token_file(tmp_path)
    _required_environment(monkeypatch)
    monkeypatch.setenv("KAL_BASE_URL", "http://kal.example.invalid")
    monkeypatch.setenv("KAL_SERVICE_TOKEN_PATH", str(token_path))
    monkeypatch.delenv("ODYSSEUS_BASE_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_TOKEN_PATH", raising=False)
    config = kal.resolve_kal_config(Settings(_env_file=None))
    assert config.base_url == "http://kal.example.invalid"
    assert config.service_token_path == token_path


def test_legacy_alias_only_configuration_works(tmp_path):
    token_path = _token_file(tmp_path)
    settings = _settings(
        token_path,
        kal_base_url=None,
        kal_service_token_path=None,
        odysseus_base_url="http://kal.example.invalid/",
        odysseus_token_path=str(token_path),
    )
    config = kal.resolve_kal_config(settings)
    assert config.base_url == "http://kal.example.invalid"
    assert config.service_token_path == token_path


def test_legacy_environment_names_reach_settings(monkeypatch, tmp_path):
    token_path = _token_file(tmp_path)
    _required_environment(monkeypatch)
    monkeypatch.delenv("KAL_BASE_URL", raising=False)
    monkeypatch.delenv("KAL_SERVICE_TOKEN_PATH", raising=False)
    monkeypatch.setenv("ODYSSEUS_BASE_URL", "http://kal.example.invalid")
    monkeypatch.setenv("ODYSSEUS_TOKEN_PATH", str(token_path))
    config = kal.resolve_kal_config(Settings(_env_file=None))
    assert config.base_url == "http://kal.example.invalid"
    assert config.service_token_path == token_path


def test_equal_canonical_and_legacy_configuration_works(tmp_path):
    token_path = _token_file(tmp_path)
    settings = _settings(
        token_path,
        kal_base_url="http://KAL.example.invalid/",
        odysseus_base_url="http://kal.example.invalid",
        odysseus_token_path=str(token_path),
    )
    assert kal.resolve_kal_config(settings).base_url == "http://kal.example.invalid"


@pytest.mark.parametrize("conflict", ["base", "path"])
def test_conflicting_canonical_legacy_configuration_fails_closed(tmp_path, conflict):
    token_path = _token_file(tmp_path)
    values = {}
    if conflict == "base":
        values["odysseus_base_url"] = "http://other.example.invalid"
    else:
        values["odysseus_token_path"] = str(tmp_path / "different-token")
    with pytest.raises(kal.KalConfigurationError) as caught:
        kal.resolve_kal_config(_settings(token_path, **values))
    message = str(caught.value)
    assert "must match" in message
    assert DUMMY_TOKEN not in message


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_http_errors_are_sanitized_and_never_emit_token(tmp_path, status):
    token_path = _token_file(tmp_path)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status, text=f"private internal detail and token {DUMMY_TOKEN}"
        )
    )
    with pytest.raises(kal.KalError) as caught:
        kal.answer_kal_technical(
            "Question", actor_identifier="beepy-user:123", settings=_settings(token_path),
            transport=transport,
        )
    assert DUMMY_TOKEN not in str(caught.value)
    assert "private internal detail" not in str(caught.value)


def test_contract_version_and_capability_fail_closed(tmp_path):
    token_path = _token_file(tmp_path)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "contract_version": "unknown",
        "capability": "technical_shared_knowledge",
        "stateless": True,
        "grounding_states": ["grounded", "no_match", "degraded"],
    }))
    with pytest.raises(kal.KalCapabilityError):
        kal.probe_kal_capability(settings=_settings(token_path), transport=transport)
