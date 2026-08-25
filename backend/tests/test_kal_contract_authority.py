"""Static compatibility check against the validated Phase 3E.6A source."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import kal


def test_client_constants_match_validated_kal_contract_source():
    candidate_value = os.environ.get("KAL_CONTRACT_CANDIDATE", "").strip()
    if not candidate_value:
        pytest.skip("KAL_CONTRACT_CANDIDATE is not set for cross-repository validation")
    candidate = Path(candidate_value)
    route_source = (candidate / "routes/beepy_integration_routes.py").read_text(encoding="utf-8")
    auth_source = (candidate / "src/beepy_service_auth.py").read_text(encoding="utf-8")

    assert 'BEEPY_CONTRACT_VERSION = "beepy-kal.v1"' in route_source
    assert 'prefix="/api/integrations/beepy"' in route_source
    assert '@router.get("/capabilities")' in route_source
    assert '@router.post("/technical-chat")' in route_source
    assert 'alias="X-Kal-Beepy-Actor"' in route_source
    assert 'alias="X-Kal-Request-ID"' in route_source
    assert 'alias="X-Kal-Business-Operation"' in route_source
    assert '["grounded", "no_match", "degraded"]' in route_source
    assert '("GET", "/api/integrations/beepy/capabilities")' in auth_source
    assert '("POST", "/api/integrations/beepy/technical-chat")' in auth_source

    assert kal.CONTRACT_VERSION == "beepy-kal.v1"
    assert kal.CAPABILITIES_ROUTE == "/api/integrations/beepy/capabilities"
    assert kal.TECHNICAL_CHAT_ROUTE == "/api/integrations/beepy/technical-chat"
    assert kal.GROUNDING_STATES == ("grounded", "no_match", "degraded")
