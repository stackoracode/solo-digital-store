"""Cloudflare Turnstile server-side verification. Tokens are always validated by the backend."""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_enabled() -> bool:
    return bool(get_settings().turnstile_secret_key)


def verify_turnstile(token: str | None, remote_ip: str) -> bool:
    settings = get_settings()
    if not settings.turnstile_secret_key:
        # Only reachable outside production (production config refuses to start without a secret).
        return True
    if not token or len(token) > 2048:
        return False
    try:
        resp = httpx.post(
            VERIFY_URL,
            data={"secret": settings.turnstile_secret_key, "response": token, "remoteip": remote_ip},
            timeout=5.0,
        )
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("turnstile verification unavailable")
        return False
    return bool(data.get("success"))
