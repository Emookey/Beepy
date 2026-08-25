from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx

EMAIL_INDEXER_SECRET = Path("/run/secrets/email-indexer.json")
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


class EmailGraphConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailIndexerConfig:
    tenant_id: str
    client_id: str
    client_secret: str


def load_email_indexer_config() -> EmailIndexerConfig:
    try:
        payload = json.loads(EMAIL_INDEXER_SECRET.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EmailGraphConfigurationError(
            f"Email indexer secret is not readable at {EMAIL_INDEXER_SECRET}."
        ) from exc
    except json.JSONDecodeError as exc:
        raise EmailGraphConfigurationError("Email indexer secret is not valid JSON.") from exc

    tenant_id = str(payload.get("tenant_id") or "").strip()
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not all((tenant_id, client_id, client_secret)):
        raise EmailGraphConfigurationError(
            "Email indexer credentials are incomplete. Configure tenant_id, client_id, and client_secret."
        )
    return EmailIndexerConfig(tenant_id, client_id, client_secret)


def email_indexer_configured() -> bool:
    try:
        load_email_indexer_config()
        return True
    except EmailGraphConfigurationError:
        return False


class GraphClient:
    def __init__(self, config: EmailIndexerConfig | None = None):
        self.config = config or load_email_indexer_config()
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = Lock()
        self.client = httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0))

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _access_token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._expires_at - 120:
                return self._token
            response = self.client.post(
                f"https://login.microsoftonline.com/{quote(self.config.tenant_id)}/oauth2/v2.0/token",
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                detail = response.text[:1000]
                raise RuntimeError(f"Microsoft token request failed: HTTP {response.status_code}: {detail}")
            payload = response.json()
            self._token = str(payload.get("access_token") or "")
            self._expires_at = time.time() + int(payload.get("expires_in") or 3600)
            if not self._token:
                raise RuntimeError("Microsoft token endpoint returned no access token.")
            return self._token

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict:
        if url.startswith("/"):
            url = GRAPH_ROOT + url
        merged = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
        }
        if headers:
            merged.update(headers)

        last_response: httpx.Response | None = None
        for attempt in range(8):
            response = self.client.request(method, url, params=params, headers=merged)
            last_response = response
            if response.status_code == 401 and attempt == 0:
                merged["Authorization"] = f"Bearer {self._access_token(force=True)}"
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 60.0) if retry_after else min(2 ** attempt, 30)
                except ValueError:
                    delay = min(2 ** attempt, 30)
                time.sleep(max(delay, 1.0))
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Graph request failed: {method} {url} -> HTTP {response.status_code}: {response.text[:1200]}"
                )
            return response.json() if response.content else {}

        assert last_response is not None
        raise RuntimeError(
            f"Graph request exhausted retries: {method} {url} -> HTTP {last_response.status_code}: {last_response.text[:1200]}"
        )

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict:
        return self.request("GET", url, params=params, headers=headers)

    def iter_pages(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        first = True
        current = url
        while current:
            payload = self.get(current, params=params if first else None, headers=headers)
            first = False
            yield payload
            current = payload.get("@odata.nextLink")
