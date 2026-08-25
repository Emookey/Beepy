from __future__ import annotations

from pathlib import Path

EMAIL_ADMINS_PATH = Path("/run/secrets/email-admins.txt")


def _normal(value: str | None) -> str:
    return str(value or "").strip().lower()


def email_admins() -> set[str]:
    try:
        raw = EMAIL_ADMINS_PATH.read_text(encoding="utf-8")
    except OSError:
        return set()
    out: set[str] = set()
    for line in raw.splitlines():
        item = line.split("#", 1)[0].strip().lower()
        if item:
            out.add(item)
    return out


def can_search_tenant_email(user_email: str | None) -> bool:
    email = _normal(user_email)
    allowed = email_admins()
    return bool(email) and (email in allowed or "*" in allowed)
