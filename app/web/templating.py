from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.config import get_settings
from app.core.i18n import SUPPORTED_LOCALES, Translator
from app.core.money import fmt
from app.core.security import csrf_token_for, new_token
from app.web.deps import CSRF_COOKIE

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def nl2br(value: str | None) -> Markup:
    """Escape user/admin text, then keep line breaks. Never renders raw HTML."""
    # Safe: every line is escaped before joining with literal <br> tags.
    return Markup("<br>".join(str(escape(line)) for line in (value or "").split("\n")))  # noqa: S704


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{round(seconds / 3600, 1)}h"


templates.env.filters["nl2br"] = nl2br
templates.env.filters["money"] = lambda amount, currency: fmt(amount, currency)
templates.env.filters["duration"] = duration
templates.env.globals["SUPPORTED_LOCALES"] = SUPPORTED_LOCALES


def set_cookie(
    response: Any, name: str, value: str, max_age: int | None = None, httponly: bool = True
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=httponly,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )


def _csrf(request: Request) -> tuple[str, str | None]:
    nonce = request.cookies.get(CSRF_COOKIE)
    new_nonce = None
    if not nonce or len(nonce) > 100:
        nonce = new_nonce = new_token(16)
    return csrf_token_for(nonce), new_nonce


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    from app.services.oauth import enabled_providers

    settings = get_settings()
    locale = getattr(request.state, "locale", "en-US")
    token, new_nonce = _csrf(request)
    store_settings = ctx.pop("store_settings", None)
    if store_settings is None and getattr(request.state, "store", None) is not None:
        from app.db import SessionLocal
        from app.services.settings_store import get_settings_map

        with SessionLocal() as db:
            store_settings = get_settings_map(db, request.state.store.id)
    context = {
        "request": request,
        "t": Translator(locale),
        "locale": locale,
        "csrf_token": token,
        "viewer": getattr(request.state, "viewer", None),
        "store": getattr(request.state, "store", None),
        "ss": store_settings or {},
        "app_env": settings.env,
        "public_base_url": settings.public_base_url.rstrip("/"),
        "turnstile_site_key": settings.turnstile_site_key if settings.turnstile_secret_key else "",
        "login_providers": enabled_providers(),
        "telegram_bot_username": settings.telegram_bot_username if settings.telegram_bot_token else "",
        **ctx,
    }
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if new_nonce:
        set_cookie(response, CSRF_COOKIE, new_nonce, max_age=60 * 60 * 24 * 7)
    if name.startswith(("shop/order", "support/ticket", "admin/")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    if not url.startswith("/") or url.startswith("//"):
        url = "/"  # open-redirect guard: only same-site relative paths
    return RedirectResponse(url, status_code=status_code)
