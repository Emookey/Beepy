from __future__ import annotations
from typing import Any
import httpx
from .config import get_settings

settings = get_settings()

def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    with httpx.Client(timeout=settings.ollama_timeout_seconds) as client:
        response = client.post(
            f"{settings.ollama_url.rstrip('/')}/api/embed",
            json={
                "model": settings.ollama_embed_model,
                "input": texts,
                "truncate": True,
                "keep_alive": "30m",
            },
        )
    response.raise_for_status()
    return response.json()["embeddings"]

def chat(messages: list[dict[str, str]], temperature: float = 0.05) -> str:
    with httpx.Client(timeout=settings.ollama_timeout_seconds) as client:
        response = client.post(
            f"{settings.ollama_url.rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_model,
                "messages": messages,
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": temperature,
                    "num_ctx": settings.ollama_context,
                },
            },
        )
    response.raise_for_status()
    return response.json().get("message", {}).get("content", "").strip()


def chat_stream(messages: list[dict[str, str]], temperature: float = 0.05):
    with httpx.stream(
        "POST",
        f"{settings.ollama_url.rstrip('/')}/api/chat",
        json={
            "model": settings.ollama_model,
            "messages": messages,
            "stream": True,
            "keep_alive": "30m",
            "options": {
                "temperature": temperature,
                "num_ctx": settings.ollama_context,
            },
        },
        timeout=settings.ollama_timeout_seconds,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            payload = __import__("json").loads(line)
            content = payload.get("message", {}).get("content", "")
            if content:
                yield content
