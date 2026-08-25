"""Provider-neutral client for Kal's scoped Beepy technical capability.

This module intentionally knows only the two routes in ``beepy-kal.v1``.  It
has no session, memory, document, tool, agent, provider, model, or business
evidence surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .config import get_settings


CONTRACT_VERSION = "beepy-kal.v1"
CAPABILITIES_ROUTE = "/api/integrations/beepy/capabilities"
TECHNICAL_CHAT_ROUTE = "/api/integrations/beepy/technical-chat"
GROUNDING_STATES = ("grounded", "no_match", "degraded")
MAX_MESSAGE_CHARS = 8000
MAX_RESPONSE_CHARS = 100_000
MAX_SOURCES = 5
MAX_SOURCE_LABEL = 120
MAX_GROUNDING_LABEL = 40

ACTOR_HEADER = "X-Kal-Beepy-Actor"
REQUEST_ID_HEADER = "X-Kal-Request-ID"
BUSINESS_OPERATION_HEADER = "X-Kal-Business-Operation"

_AUDIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_TOKEN_RE = re.compile(r"^ody_[A-Za-z0-9_-]{8,96}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9 _.,:@()#&+\-]{1,120}$")
_GROUNDING_RE = re.compile(r"^[A-Za-z0-9 _.,:@()#&+\-]{1,40}$")
_LEGACY_TOKEN_PATH = "/run/secrets/odysseus.token"


class KalError(RuntimeError):
    """Base class whose messages are safe to display operationally."""


class KalConfigurationError(KalError):
    pass


class KalUnavailableError(KalError):
    pass


class KalAuthenticationError(KalError):
    pass


class KalCapabilityError(KalError):
    pass


class KalContractError(KalError):
    pass


@dataclass(frozen=True)
class KalIntegrationConfig:
    base_url: str
    service_token_path: Path
    timeout_seconds: float


@dataclass(frozen=True)
class KalSource:
    label: str
    grounding: str

    def as_beepy_source(self) -> dict[str, str]:
        return {
            "sourceType": "kal-shared-knowledge",
            "title": self.label,
            "grounding": self.grounding,
        }


@dataclass(frozen=True)
class KalTechnicalResponse:
    response: str
    shared_knowledge_used: bool
    grounding_status: str
    sources: tuple[KalSource, ...]
    request_id: str


@dataclass(frozen=True)
class KalCapability:
    capability: str
    stateless: bool
    grounding_states: tuple[str, ...]


def _setting(settings: Any, name: str) -> str | None:
    value = getattr(settings, name, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _resolve_alias(
    canonical: str | None,
    legacy: str | None,
    *,
    canonical_name: str,
    legacy_name: str,
    normalize: Callable[[str], str] = lambda value: value,
) -> str | None:
    canonical_semantic = normalize(canonical) if canonical else None
    legacy_semantic = normalize(legacy) if legacy else None
    if canonical_semantic and legacy_semantic and canonical_semantic != legacy_semantic:
        raise KalConfigurationError(
            f"Conflicting configuration: {canonical_name} and {legacy_name} must match."
        )
    return canonical_semantic or legacy_semantic


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise KalConfigurationError("Invalid configuration: KAL_BASE_URL.")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _normalize_token_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise KalConfigurationError("Invalid configuration: KAL_SERVICE_TOKEN_PATH.")
    return str(path)


def resolve_kal_config(settings: Any | None = None) -> KalIntegrationConfig:
    settings = settings or get_settings()
    base_url = _resolve_alias(
        _setting(settings, "kal_base_url"),
        _setting(settings, "odysseus_base_url"),
        canonical_name="KAL_BASE_URL",
        legacy_name="ODYSSEUS_BASE_URL",
        normalize=_normalize_base_url,
    )
    if not base_url:
        raise KalConfigurationError("Missing configuration: KAL_BASE_URL.")

    token_path = _resolve_alias(
        _setting(settings, "kal_service_token_path"),
        _setting(settings, "odysseus_token_path"),
        canonical_name="KAL_SERVICE_TOKEN_PATH",
        legacy_name="ODYSSEUS_TOKEN_PATH",
        normalize=_normalize_token_path,
    ) or _LEGACY_TOKEN_PATH

    try:
        timeout = float(getattr(settings, "kal_timeout_seconds", 180.0))
    except (TypeError, ValueError) as exc:
        raise KalConfigurationError("Invalid configuration: KAL_TIMEOUT_SECONDS.") from exc
    if timeout < 1 or timeout > 600:
        raise KalConfigurationError("Invalid configuration: KAL_TIMEOUT_SECONDS.")
    return KalIntegrationConfig(base_url, Path(token_path), timeout)


def _read_service_token(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KalConfigurationError(
            "The scoped Kal service credential is missing or unreadable."
        ) from exc
    if not raw:
        raise KalConfigurationError("The scoped Kal service credential is empty.")
    if raw.startswith("{") or raw.startswith("["):
        raise KalConfigurationError(
            "Legacy per-user token maps are not accepted for Kal technical mode."
        )
    if not _TOKEN_RE.fullmatch(raw):
        raise KalConfigurationError("The scoped Kal service credential is malformed.")
    return raw


def beepy_actor_audit_id(user_email: str) -> str:
    """Create a stable non-authorizing actor label without disclosing email."""

    normalized = str(user_email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise KalContractError("Authenticated Beepy actor metadata is invalid.")
    return "beepy-user:" + sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _audit_value(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not _AUDIT_RE.fullmatch(value):
        raise KalContractError(f"Invalid {field_name} audit metadata.")
    return value


def _client(
    config: KalIntegrationConfig,
    token: str,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(config.timeout_seconds, connect=min(10.0, config.timeout_seconds)),
        follow_redirects=False,
        transport=transport,
    )


def _request(client: httpx.Client, method: str, route: str, **kwargs: Any) -> httpx.Response:
    try:
        response = client.request(method, route, **kwargs)
    except httpx.HTTPError as exc:
        raise KalUnavailableError("Kal technical service is unavailable.") from exc
    if response.status_code in {401, 403}:
        raise KalAuthenticationError("Kal rejected the scoped Beepy service credential.")
    if response.status_code == 404:
        raise KalCapabilityError("Kal does not provide the required Beepy capability.")
    if response.status_code in {400, 409, 422}:
        raise KalContractError("Kal rejected the Beepy technical request contract.")
    if response.status_code >= 500:
        raise KalUnavailableError("Kal technical service is unavailable.")
    if not response.is_success:
        raise KalCapabilityError("Kal technical capability request failed.")
    return response


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise KalContractError("Kal returned an invalid technical response.") from exc
    if not isinstance(payload, dict):
        raise KalContractError("Kal returned an invalid technical response.")
    return payload


def _contract_version(payload: dict[str, Any]) -> None:
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise KalCapabilityError("Kal Beepy contract version is unsupported.")


def probe_kal_capability(
    *,
    settings: Any | None = None,
    transport: httpx.BaseTransport | None = None,
) -> KalCapability:
    config = resolve_kal_config(settings)
    token = _read_service_token(config.service_token_path)
    with _client(config, token, transport) as client:
        payload = _object(_request(client, "GET", CAPABILITIES_ROUTE))
    _contract_version(payload)
    states = payload.get("grounding_states")
    if (
        payload.get("capability") != "technical_shared_knowledge"
        or payload.get("stateless") is not True
        or states != list(GROUNDING_STATES)
    ):
        raise KalCapabilityError("Kal Beepy capability response is incompatible.")
    return KalCapability(payload["capability"], True, tuple(states))


def _source(value: Any) -> KalSource:
    if not isinstance(value, dict) or set(value) != {"label", "grounding"}:
        raise KalContractError("Kal returned unsafe source metadata.")
    label = value.get("label")
    grounding = value.get("grounding")
    if not isinstance(label, str) or not _SOURCE_RE.fullmatch(label):
        raise KalContractError("Kal returned unsafe source metadata.")
    if "/" in label or "\\" in label or not isinstance(grounding, str) or not _GROUNDING_RE.fullmatch(grounding):
        raise KalContractError("Kal returned unsafe source metadata.")
    return KalSource(label, grounding)


def answer_kal_technical(
    message: str,
    *,
    actor_identifier: str,
    request_id: str | None = None,
    settings: Any | None = None,
    transport: httpx.BaseTransport | None = None,
) -> KalTechnicalResponse:
    """Submit one isolated stateless technical turn."""

    message = str(message or "").strip()
    if not message or len(message) > MAX_MESSAGE_CHARS or any(
        ord(char) < 32 and char not in "\n\r\t" for char in message
    ):
        raise KalContractError("Technical message is outside the Kal contract bounds.")

    actor = _audit_value(actor_identifier, "actor")
    correlation = _audit_value(request_id or str(uuid4()), "request identifier")
    operation = _audit_value("technical-chat", "business operation")
    config = resolve_kal_config(settings)
    token = _read_service_token(config.service_token_path)
    headers = {
        ACTOR_HEADER: actor,
        REQUEST_ID_HEADER: correlation,
        BUSINESS_OPERATION_HEADER: operation,
    }
    with _client(config, token, transport) as client:
        payload = _object(
            _request(
                client,
                "POST",
                TECHNICAL_CHAT_ROUTE,
                json={"message": message},
                headers=headers,
            )
        )

    _contract_version(payload)
    response_text = payload.get("response")
    used = payload.get("shared_knowledge_used")
    status = payload.get("grounding_status")
    request_value = payload.get("request_id")
    raw_sources = payload.get("sources")
    if (
        not isinstance(response_text, str)
        or not response_text.strip()
        or len(response_text) > MAX_RESPONSE_CHARS
        or type(used) is not bool
        or status not in GROUNDING_STATES
        or not isinstance(request_value, str)
        or not _AUDIT_RE.fullmatch(request_value)
        or not isinstance(raw_sources, list)
        or len(raw_sources) > MAX_SOURCES
        or used != (status == "grounded")
        or (status != "grounded" and raw_sources)
    ):
        raise KalContractError("Kal returned an invalid technical response.")
    sources = tuple(_source(item) for item in raw_sources)
    return KalTechnicalResponse(response_text.strip(), used, status, sources, request_value)
