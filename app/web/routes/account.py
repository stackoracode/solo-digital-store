"""Sign-in: e-mail code, Google / generic OIDC (Authlib), Telegram widget; staff password + TOTP."""

from __future__ import annotations

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.core.security import decrypt, verify_totp
from app.core.turnstile import verify_turnstile
from app.db import get_db
from app.models.orders import Order
from app.models.stores import Store
from app.models.users import User, UserIdentity
from app.services.audit import audit
from app.services.auth import (
    AuthError,
    create_session,
    login_with_identity,
    request_email_code,
    revoke_session,
    staff_password_login,
    verify_email_code,
    verify_telegram_login,
)
from app.services.oauth import enabled_providers, oauth_registry, provider_key
from app.services.orders import OrderError
from app.web.deps import (
    SESSION_COOKIE,
    Viewer,
    csrf_form,
    form_str,
    get_store,
    get_viewer,
    rate_limit,
    require_user,
)
from app.web.templating import redirect, render, set_cookie

router = APIRouter(prefix="/account")


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value[:300]
    return "/account"


def _login_response(request: Request, db: Session, user: User, next_url: str, *, mfa_passed: bool = False):  # type: ignore[no-untyped-def]
    revoke_session(db, request.cookies.get(SESSION_COOKIE))  # session fixation defence
    token = create_session(
        db,
        user,
        ip=request.state.client_ip,
        user_agent=request.headers.get("user-agent", ""),
        mfa_passed=mfa_passed,
    )
    db.commit()
    response = redirect(next_url)
    set_cookie(response, SESSION_COOKIE, token, max_age=get_settings().session_ttl_hours * 3600)
    return response


@router.get("/login")
def login_page(
    request: Request, next: str = "", store: Store = Depends(get_store), viewer: Viewer = Depends(get_viewer)
):
    if viewer.user is not None:
        return redirect(_safe_next(next))
    return render(request, "account/login.html", next=_safe_next(next), step="email", email="", error=None)


@router.post("/login/code")
def login_request_code(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "login_code", 5, 900)
    next_url = _safe_next(form_str(form, "next", 300))
    email = form_str(form, "email", 254)
    if not verify_turnstile(form_str(form, "cf-turnstile-response", 2048), request.state.client_ip):
        return render(
            request,
            "account/login.html",
            status_code=400,
            next=next_url,
            step="email",
            email=email,
            error="turnstile",
        )
    try:
        request_email_code(db, email, ip=request.state.client_ip, locale=request.state.locale)
        db.commit()
    except OrderError:
        return render(
            request,
            "account/login.html",
            status_code=400,
            next=next_url,
            step="email",
            email=email,
            error="invalid_email",
        )
    except AuthError as exc:
        db.rollback()
        return render(
            request,
            "account/login.html",
            status_code=429,
            next=next_url,
            step="email",
            email=email,
            error=exc.code,
        )
    return render(request, "account/login.html", next=next_url, step="code", email=email, error=None)


@router.post("/login/verify")
def login_verify_code(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "login_verify", 10, 900)
    next_url = _safe_next(form_str(form, "next", 300))
    email = form_str(form, "email", 254)
    try:
        user = verify_email_code(db, email, form_str(form, "code", 10))
    except (AuthError, OrderError) as exc:
        db.commit()  # persist the attempt counter
        code = exc.code if isinstance(exc, AuthError) else "code_invalid"
        return render(
            request,
            "account/login.html",
            status_code=400,
            next=next_url,
            step="code",
            email=email,
            error=code,
        )
    return _login_response(request, db, user, next_url)


@router.get("/oauth/{provider}")
async def oauth_start(request: Request, provider: str, next: str = ""):
    rate_limit(request, "oauth_start", 20, 600)
    if provider not in {p["name"] for p in enabled_providers()}:
        raise HTTPException(404, "Login provider not enabled")
    client = oauth_registry().create_client(provider)
    request.session["oauth_next"] = _safe_next(next)
    redirect_uri = f"{get_settings().public_base_url.rstrip('/')}/account/oauth/{provider}/callback"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(request: Request, provider: str):
    if provider not in {p["name"] for p in enabled_providers()}:
        raise HTTPException(404, "Login provider not enabled")
    client = oauth_registry().create_client(provider)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(400, f"Sign-in failed: {exc.error}") from exc
    claims = token.get("userinfo") or {}
    if not claims.get("sub"):
        raise HTTPException(400, "Sign-in failed: no subject")
    next_url = _safe_next(request.session.pop("oauth_next", None))
    from starlette.concurrency import run_in_threadpool

    def _finish():  # type: ignore[no-untyped-def]
        from app.db import SessionLocal

        with SessionLocal() as db:
            try:
                user = login_with_identity(
                    db,
                    provider=provider_key(provider),
                    subject=str(claims["sub"]),
                    email=claims.get("email"),
                    email_verified=claims.get("email_verified") is True,
                    display_name=str(claims.get("name") or claims.get("email") or "")[:80],
                )
            except AuthError as exc:
                db.rollback()
                raise HTTPException(403, exc.code) from exc
            return _login_response(request, db, user, next_url)

    return await run_in_threadpool(_finish)


@router.get("/telegram/callback")
def telegram_callback(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    rate_limit(request, "telegram", 20, 600)
    s = get_settings()
    try:
        fields = verify_telegram_login(
            dict(request.query_params), s.telegram_bot_token, s.telegram_auth_max_age_seconds
        )
        name = " ".join(x for x in (fields.get("first_name"), fields.get("last_name")) if x) or fields.get(
            "username", ""
        )
        user = login_with_identity(
            db, provider="telegram", subject=fields["id"], email=None, email_verified=False, display_name=name
        )
    except AuthError as exc:
        db.rollback()
        raise HTTPException(403, f"Telegram sign-in failed: {exc.code}") from exc
    return _login_response(request, db, user, "/account")


@router.post("/logout")
def logout(request: Request, form: FormData = Depends(csrf_form), db: Session = Depends(get_db)):
    revoke_session(db, request.cookies.get(SESSION_COOKIE))
    response = redirect("/")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("")
def account_home(
    request: Request,
    store: Store = Depends(get_store),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    orders = (
        db.execute(
            select(Order)
            .where(Order.user_id == user.id, Order.store_id == store.id)
            .order_by(Order.id.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    identities = db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id)).scalars().all()
    unclaimed = 0
    if user.email and user.email_verified_at:
        unclaimed = len(
            db.execute(
                select(Order.id).where(
                    Order.email == user.email, Order.user_id.is_(None), Order.store_id == store.id
                )
            ).all()
        )
    return render(
        request, "account/home.html", user=user, orders=orders, identities=identities, unclaimed=unclaimed
    )


@router.post("/claim-orders")
def claim_orders(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach guest orders placed with this account's *verified* e-mail address."""
    if user.email and user.email_verified_at:
        for order in db.execute(
            select(Order).where(
                Order.email == user.email, Order.user_id.is_(None), Order.store_id == store.id
            )
        ).scalars():
            order.user_id = user.id
        db.commit()
    return redirect("/account")


@router.post("/profile")
def update_profile(
    request: Request,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    user.display_name = form_str(form, "display_name", 80)
    locale = form_str(form, "locale", 10)
    if locale in ("en-US", "zh-CN"):
        user.locale = locale
    db.commit()
    return redirect("/account")


# Staff ------------------------------------------------------------------------------------------


@router.get("/staff-login")
def staff_login_page(request: Request, next: str = "", viewer: Viewer = Depends(get_viewer)):
    return render(request, "account/staff_login.html", next=_safe_next(next or "/admin"), error=None)


@router.post("/staff-login")
def staff_login(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
):
    rate_limit(request, "staff_login", 10, 900)
    next_url = _safe_next(form_str(form, "next", 300) or "/admin")
    if not verify_turnstile(form_str(form, "cf-turnstile-response", 2048), request.state.client_ip):
        return render(request, "account/staff_login.html", status_code=400, next=next_url, error="turnstile")
    try:
        user = staff_password_login(db, form_str(form, "email", 254), form_str(form, "password", 200))
    except AuthError as exc:
        audit(
            db,
            "staff_login_failed",
            actor_ip=request.state.client_ip,
            details={"email": form_str(form, "email", 254)[:3] + "***", "reason": exc.code},
        )
        db.commit()
        return render(request, "account/staff_login.html", status_code=400, next=next_url, error=exc.code)
    audit(db, "staff_login", actor_user_id=user.id, actor_ip=request.state.client_ip)
    target = "/account/mfa?next=" + next_url if user.totp_enabled else next_url
    return _login_response(request, db, user, target)


@router.get("/mfa")
def mfa_page(request: Request, next: str = "", viewer: Viewer = Depends(get_viewer)):
    if viewer.user is None:
        return redirect("/account/staff-login")
    return render(request, "account/mfa.html", next=_safe_next(next or "/admin"), error=None)


@router.post("/mfa")
def mfa_verify(
    request: Request,
    form: FormData = Depends(csrf_form),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "mfa", 10, 900)
    next_url = _safe_next(form_str(form, "next", 300) or "/admin")
    if viewer.user is None or viewer.session is None or not viewer.user.totp_secret_enc:
        return redirect("/account/staff-login")
    if not verify_totp(decrypt(viewer.user.totp_secret_enc), form_str(form, "code", 10)):
        audit(db, "mfa_failed", actor_user_id=viewer.user.id, actor_ip=request.state.client_ip)
        db.commit()
        return render(request, "account/mfa.html", status_code=400, next=next_url, error="code_invalid")
    viewer.session.mfa_passed = True
    db.merge(viewer.session)
    audit(db, "mfa_passed", actor_user_id=viewer.user.id, actor_ip=request.state.client_ip)
    db.commit()
    return redirect(next_url)
