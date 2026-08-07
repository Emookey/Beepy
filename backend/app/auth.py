from dataclasses import dataclass
import httpx
from fastapi import Header, HTTPException
from .config import get_settings

settings = get_settings()

@dataclass
class User:
    id: str
    email: str
    name: str

async def require_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Microsoft access token is required.")
    token = authorization[7:].strip()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id,displayName,mail,userPrincipalName"},
        )
    if response.status_code != 200:
        raise HTTPException(401, "Microsoft rejected the access token.")
    data = response.json()
    email = (data.get("mail") or data.get("userPrincipalName") or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    if domain != settings.allowed_email_domain.lower():
        raise HTTPException(403, "This Microsoft account is not permitted.")
    return User(
        id=data.get("id", ""),
        email=email,
        name=data.get("displayName") or email,
    )
