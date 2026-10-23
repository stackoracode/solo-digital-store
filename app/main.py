"""ASGI entrypoint: `uvicorn app.main:app`."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import EdgeMiddleware
from app.web.deps import REF_COOKIE, MfaRequired, NotAuthenticated, get_locale
from app.web.templating import redirect, render, set_cookie

REF_RE = re.compile(r"^[A-Z0-9]{4,24}$")


def _load_verified_domains() -> set[str]:
    from app.db import SessionLocal
    from app.models.stores import StoreDomain

    with SessionLocal() as db:
        return set(db.execute(select(StoreDomain.hostname).where(StoreDomain.status == "verified")).scalars())


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title=settings.app_name,
        docs_url=None if settings.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )

    from app.web.routes import account, agent, api, orders, shop, support, webhooks
    from app.web.routes.admin import router as admin_router

    for module in (shop, orders, account, support, agent, webhooks, api):
        app.include_router(module.router)
    app.include_router(admin_router)

    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    media_dir = Path(settings.local_media_dir)
    if settings.storage_backend == "local":
        media_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    @app.middleware("http")
    async def locale_and_referral(request: Request, call_next):  # type: ignore[no-untyped-def]
        get_locale(request)
        response = await call_next(request)
        ref = request.query_params.get("ref", "")
        if request.method == "GET" and ref and REF_RE.match(ref):
            # Last-click attribution for 30 days; validated against active agents at checkout.
            set_cookie(response, REF_COOKIE, ref, max_age=60 * 60 * 24 * 30)
        lang = request.query_params.get("lang")
        if lang in ("en-US", "zh-CN"):
            set_cookie(response, "lang", lang, max_age=60 * 60 * 24 * 365)
        return response

    # Authlib keeps OAuth state/nonce here. Signed, short-lived, host-only cookie.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="oauth_state",
        max_age=600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(EdgeMiddleware, settings=settings, domain_loader=_load_verified_domains)

    @app.exception_handler(NotAuthenticated)
    async def _not_auth(request: Request, exc: NotAuthenticated):  # type: ignore[no-untyped-def]
        target = "/account/staff-login" if exc.admin else "/account/login"
        return redirect(f"{target}?next={exc.next_url}")

    @app.exception_handler(MfaRequired)
    async def _mfa(request: Request, exc: MfaRequired):  # type: ignore[no-untyped-def]
        return redirect("/admin/mfa/setup" if exc.setup else "/account/mfa")

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        if request.url.path.startswith(("/api/", "/webhooks/")):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return render(
            request, "error.html", status_code=exc.status_code, status=exc.status_code, detail=exc.detail
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> PlainTextResponse:
        from sqlalchemy import text

        from app.db import SessionLocal

        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return PlainTextResponse("ready")

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(
            "User-agent: *\nDisallow: /admin\nDisallow: /o/\nDisallow: /account\nDisallow: /support/t/\n"
        )

    return app


app = create_app()
