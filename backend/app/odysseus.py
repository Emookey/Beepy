from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import logging
import json

import httpx
from .config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Kept out of .env by design. The API token itself is mounted read-only by
# Docker; only non-secret local service details live in this module.
ODYSSEUS_BASE_URL = settings.odysseus_base_url
ODYSSEUS_TOKEN_PATH = Path("/run/secrets/odysseus.token")
ODYSSEUS_MODEL = "qwen2.5:3b-project"
ODYSSEUS_ENDPOINT_ID = settings.odysseus_endpoint_id
ODYSSEUS_OLLAMA_BASE = settings.odysseus_ollama_base


class OdysseusError(RuntimeError):
    pass


def _odysseus_username(user_email: str) -> str:
    email = str(user_email or "").strip().lower()

    if not email or "@" not in email:
        raise OdysseusError("Authenticated MBC user does not have a valid email address.")

    # MBC Entra accounts map to the corresponding Odysseus username.
    # Example: alice@your-domain.example -> jerry
    username = email.split("@", 1)[0].strip()

    if not username:
        raise OdysseusError("Could not determine the Odysseus username.")

    return username


def _read_token(user_email: str | None = None) -> str:
    try:
        raw = ODYSSEUS_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OdysseusError(
            f"Odysseus token file is not readable at {ODYSSEUS_TOKEN_PATH}."
        ) from exc

    # Legacy single-token format. Keep this only for a non-user health probe.
    if raw.startswith("ody_"):
        if user_email:
            raise OdysseusError(
                "Per-user Odysseus authentication is not configured yet."
            )
        return raw

    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OdysseusError(
            "Odysseus token file is neither a valid API token nor a valid user-token map."
        ) from exc

    if not isinstance(tokens, dict) or not tokens:
        raise OdysseusError("Odysseus user-token map is empty or invalid.")

    if user_email:
        username = _odysseus_username(user_email)

        token = (
            tokens.get(username)
            or tokens.get(str(user_email).strip().lower())
        )

        if not token:
            raise OdysseusError(
                f"No Odysseus API token is configured for user '{username}'."
            )

        if not isinstance(token, str) or not token.startswith("ody_"):
            raise OdysseusError(
                f"Invalid Odysseus API token configured for user '{username}'."
            )

        logger.info(
            "MBC authenticated user %s mapped to Odysseus user %s",
            user_email,
            username,
        )

        return token

    # Health checks can use a configured token without impersonating
    # a user-facing chat request.
    for token in tokens.values():
        if isinstance(token, str) and token.startswith("ody_"):
            return token

    raise OdysseusError("No valid Odysseus API token exists in the token map.")


def _client(user_email: str | None = None) -> httpx.Client:
    token = _read_token(user_email)

    return httpx.Client(
        base_url=ODYSSEUS_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(180.0, connect=10.0),
        follow_redirects=False,
    )


def _raise_for_odysseus(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
        detail = payload.get("detail") or payload.get("error") or response.text
    except Exception:
        detail = response.text
    detail = str(detail).strip()[:800]
    raise OdysseusError(
        f"Odysseus {action} failed with HTTP {response.status_code}: {detail}"
    )


def _create_session(client: httpx.Client) -> str:
    name = f"[MBC Beepy] Tech {str(uuid4())[:8]}"
    data = {
        "name": name,
        "endpoint_id": ODYSSEUS_ENDPOINT_ID,
        "model": ODYSSEUS_MODEL,
        "rag": "true",
    }
    response = client.post("/api/session", data=data)

    # If the saved endpoint ID ever changes, retry against the same local
    # Ollama endpoint directly. The dedicated token belongs to an Odysseus
    # admin account, while the normal path above remains the preferred route.
    if response.status_code == 400 and "endpoint" in response.text.lower():
        data = {
            "name": name,
            "endpoint_url": ODYSSEUS_OLLAMA_BASE,
            "model": ODYSSEUS_MODEL,
            "rag": "true",
        }
        response = client.post("/api/session", data=data)

    _raise_for_odysseus(response, "session creation")
    payload = response.json()
    session_id = payload.get("id") or payload.get("session_id") or payload.get("session")
    if not session_id:
        raise OdysseusError("Odysseus created a session but returned no session ID.")
    return str(session_id)


def _conversation_context(history: list[dict]) -> str:
    if not history:
        return ""
    chunks: list[str] = []
    for item in history[-6:]:
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        label = "User" if role == "user" else "Beepy"
        chunks.append(f"{label}: {content[:1200]}")
    return "\n\n".join(chunks)[-5000:]


def answer_odysseus_tech(
    question: str,
    history: list[dict] | None = None,
    user_email: str | None = None,
) -> str:
    """Ask qwen2.5:3b-project through the existing Odysseus RAG pipeline."""
    history = history or []
    context = _conversation_context(history)
    if context:
        message = (
            "Use the prior Beepy conversation below only when it helps interpret "
            "the current question. Use your configured RAG knowledge normally.\n\n"
            "--- PRIOR BEEPY CONTEXT ---\n"
            f"{context}\n"
            "--- END PRIOR CONTEXT ---\n\n"
            "CURRENT QUESTION:\n"
            f"{question}"
        )
    else:
        message = question

    session_id: str | None = None
    with _client(user_email) as client:
        try:
            session_id = _create_session(client)
            response = client.post(
                "/api/chat",
                json={"message": message, "session": session_id},
            )
            _raise_for_odysseus(response, "chat")
            payload = response.json()
            answer = str(payload.get("response") or "").strip()
            if not answer:
                raise OdysseusError("Odysseus returned an empty technical answer.")
            return answer
        finally:
            if session_id:
                try:
                    cleanup = client.delete(f"/api/session/{session_id}")
                    if not cleanup.is_success:
                        logger.warning(
                            "Could not remove temporary Odysseus session %s: HTTP %s",
                            session_id,
                            cleanup.status_code,
                        )
                except Exception as exc:
                    logger.warning(
                        "Could not remove temporary Odysseus session %s: %s",
                        session_id,
                        exc,
                    )


def probe_odysseus() -> dict:
    """Authenticate to Odysseus without spending model tokens."""
    with _client() as client:
        response = client.get("/api/sessions")
        _raise_for_odysseus(response, "authentication probe")
    return {
        "ok": True,
        "base_url": ODYSSEUS_BASE_URL,
        "model": ODYSSEUS_MODEL,
        "rag": True,
    }
