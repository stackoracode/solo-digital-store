"""Request-scoped dependencies: store resolution, locale, session user, CSRF-checked form, admin guard."""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.core.i18n import negotiate
from app.core.ratelimit import get_limiter
from app.core.security import csrf_valid
from app.db import get_db
from app.models.stores import Store, StoreDomain
from app.models.users import User, UserSession
from app.services.auth import load_session

SESSION_COOKIE = "sid"
CSRF_COOKIE = "csrf"
LANG_COOKIE = "lang"
REF_COOKIE = "ref"


class NotAuthenticated(Exception):
    def __init__(self, next_url: str, admin: bool = False) -> None:
        self.next_url = next_url
        self.admin = admin


class MfaRequired(Exception):
    def __init__(self, setup: bool) -> None:
        self.setup = setup


_store_cache: dict[str, tuple[float, int | None]] = {}


def resolve_store(db: Session, host: str) -> Store | None:
    """Default store for primary hosts; otherwise only *verified* custom domains map to a store."""
    settings = get_settings()
    now = time.monotonic()
    cached = _store_cache.get(host)
    store_id: int | None
    if cached and now - cached[0] < 30:
        store_id = cached[1]
    else:
        if host in {h.lower() for h in settings.primary_hosts}:
            store_id = db.scalar(select(Store.id).where(Store.is_default.is_(True)))
        else:
            store_id = db.scalar(
                select(StoreDomain.store_id).where(
                    StoreDomain.hostname == host, StoreDomain.status == "verified"
                )
            )
        _store_cache[host] = (now, store_id)
    if store_id is None:
        return None
    store = db.get(Store, store_id)
    return store if store is not None and store.status == "active" else None


def clear_store_cache() -> None:
    _store_cache.clear()


def get_store(request: Request, db: Session = Depends(get_db)) -> Store:
    store = resolve_store(db, getattr(request.state, "host", "") or "")
    if store is None:
        raise HTTPException(status_code=421, detail="Unknown host")
    request.state.store = store
    return store


def get_locale(request: Request) -> str:
    locale = negotiate(
        request.query_params.get("lang"),
        request.cookies.get(LANG_COOKIE),
        request.headers.get("accept-language"),
    )
    request.state.locale = locale
    return locale


@dataclass
class Viewer:
    user: User | None
    session: UserSession | None

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.is_admin)


def get_viewer(request: Request, db: Session = Depends(get_db)) -> Viewer:
    loaded = load_session(db, request.cookies.get(SESSION_COOKIE))
    viewer = Viewer(user=loaded[1], session=loaded[0]) if loaded else Viewer(None, None)
    request.state.viewer = viewer
    return viewer


def require_user(request: Request, viewer: Viewer = Depends(get_viewer)) -> User:
    if viewer.user is None:
        raise NotAuthenticated(str(request.url.path))
    return viewer.user


def require_admin(request: Request, viewer: Viewer = Depends(get_viewer)) -> User:
    user = viewer.user
    if user is None or not user.is_admin or viewer.session is None:
        raise NotAuthenticated(str(request.url.path), admin=True)
    settings = get_settings()
    if settings.admin_require_mfa and not user.totp_enabled:
        if not request.url.path.startswith("/admin/mfa"):
            raise MfaRequired(setup=True)
    elif user.totp_enabled and not viewer.session.mfa_passed:
        raise MfaRequired(setup=False)
    return user


def require_owner(user: User = Depends(require_admin)) -> User:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return user


async def csrf_form(request: Request) -> FormData:
    """Parse the form (bounded) and enforce the CSRF token + same-origin checks."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = origin.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
        if host != getattr(request.state, "host", ""):
            raise HTTPException(status_code=403, detail="Cross-origin request blocked")
    form = await request.form(max_files=10, max_fields=300)
    token = form.get("csrf_token")
    if not csrf_valid(request.cookies.get(CSRF_COOKIE), token if isinstance(token, str) else None):
        raise HTTPException(status_code=403, detail="CSRF validation failed; reload the page and retry")
    return form


def rate_limit(request: Request, bucket: str, limit: int, window: int, extra: str = "") -> None:
    ip = getattr(request.state, "client_ip", "unknown")
    if not get_limiter().hit(f"{bucket}:{ip}:{extra}", limit, window):
        raise HTTPException(status_code=429, detail="Too many requests; please slow down")


def form_str(form: FormData, key: str, max_len: int = 1000) -> str:
    value = form.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]
