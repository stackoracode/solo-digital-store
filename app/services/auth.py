"""Authentication: server-side sessions, passwordless e-mail codes, OAuth/OIDC and Telegram identities.

Customers: e-mail one-time code, Google (OIDC), Telegram Login Widget, optional generic OIDC (e.g. Logto).
Staff/owner: password + mandatory TOTP. External identities never grant staff roles.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import encrypt, keyed_hash, new_token, token_hash, verify_password
from app.db import utcnow
from app.jobs.queue import enqueue
from app.models.users import EmailLoginCode, User, UserIdentity, UserSession
from app.services.orders import OrderError, normalize_email

MAX_FAILED_LOGINS = 8
LOCK_MINUTES = 15
CODES_PER_EMAIL_PER_HOUR = 5


class AuthError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Sessions --------------------------------------------------------------------------------------


def create_session(db: Session, user: User, *, ip: str, user_agent: str, mfa_passed: bool = False) -> str:
    token = new_token(32)
    now = utcnow()
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    if user.is_admin:
        ttl = min(ttl, timedelta(hours=12))
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + ttl,
            mfa_passed=mfa_passed,
            ip=ip[:64],
            user_agent=user_agent[:255],
        )
    )
    user.last_login_at = now
    return token


def load_session(db: Session, token: str | None) -> tuple[UserSession, User] | None:
    if not token or len(token) > 128:
        return None
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash(token), UserSession.revoked_at.is_(None))
    ).first()
    if row is None:
        return None
    sess, user = row
    now = utcnow()
    if sess.expires_at < now or not user.is_active:
        return None
    if user.is_admin and sess.last_seen_at < now - timedelta(
        minutes=get_settings().admin_session_idle_minutes
    ):
        return None
    if (now - sess.last_seen_at).total_seconds() > 60:
        db.execute(update(UserSession).where(UserSession.id == sess.id).values(last_seen_at=now))
        db.commit()
    return sess, user


def revoke_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(
            update(UserSession).where(UserSession.token_hash == token_hash(token)).values(revoked_at=utcnow())
        )
        db.commit()


def revoke_all_sessions(db: Session, user_id: int) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


# Staff password login ----------------------------------------------------------------------------


def staff_password_login(db: Session, email: str, password: str) -> User:
    try:
        email = normalize_email(email)
    except OrderError as exc:
        raise AuthError("invalid_credentials") from exc
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        raise AuthError("locked")
    ok = verify_password(password, user.password_hash if user else None)
    if user is None or not ok or not user.is_admin or not user.is_active:
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                user.failed_logins = 0
            db.commit()
        raise AuthError("invalid_credentials")
    user.failed_logins = 0
    user.locked_until = None
    return user


# E-mail one-time codes ---------------------------------------------------------------------------


def _code_hash(email: str, code: str) -> str:
    return keyed_hash(f"{email}:{code}", "login-code")


def request_email_code(db: Session, raw_email: str, *, ip: str, locale: str) -> None:
    """Always behaves the same whether or not an account exists (no enumeration)."""
    email = normalize_email(raw_email)
    recent = (
        db.scalar(
            select(func.count())
            .select_from(EmailLoginCode)
            .where(EmailLoginCode.email == email, EmailLoginCode.created_at > utcnow() - timedelta(hours=1))
        )
        or 0
    )
    if recent >= CODES_PER_EMAIL_PER_HOUR:
        raise AuthError("too_many_codes")
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = utcnow()
    db.execute(
        update(EmailLoginCode)
        .where(EmailLoginCode.email == email, EmailLoginCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    db.add(
        EmailLoginCode(
            email=email,
            code_hash=_code_hash(email, code),
            purpose="login",
            created_at=now,
            expires_at=now + timedelta(minutes=get_settings().email_code_ttl_minutes),
            ip=ip[:64],
        )
    )
    db.flush()
    enqueue(
        db,
        "send_email",
        {"template": "login_code", "email": email, "locale": locale, "code_enc": encrypt(code)},
        max_attempts=3,
    )


def verify_email_code(db: Session, raw_email: str, code: str) -> User:
    email = normalize_email(raw_email)
    row = db.execute(
        select(EmailLoginCode)
        .where(EmailLoginCode.email == email, EmailLoginCode.consumed_at.is_(None))
        .order_by(EmailLoginCode.id.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    now = utcnow()
    if row is None or row.expires_at < now:
        raise AuthError("code_invalid")
    row.attempts += 1
    if row.attempts > get_settings().email_code_max_attempts:
        row.consumed_at = now
        db.commit()
        raise AuthError("code_invalid")
    code = (code or "").strip()
    if not hmac.compare_digest(row.code_hash, _code_hash(email, code)):
        db.commit()
        raise AuthError("code_invalid")
    row.consumed_at = now
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(
            email=email, role="customer", email_verified_at=now, display_name=email.split("@")[0][:80]
        )
        db.add(user)
        db.flush()
    else:
        if user.is_admin:
            # Staff must use password + TOTP; codes cannot be used to reach the admin area.
            raise AuthError("use_staff_login")
        user.email_verified_at = user.email_verified_at or now
    return user


# External identities -----------------------------------------------------------------------------


def login_with_identity(
    db: Session,
    *,
    provider: str,
    subject: str,
    email: str | None,
    email_verified: bool,
    display_name: str,
) -> User:
    """Find or create the customer for an external identity.

    Auto-linking to an existing account by e-mail happens only when the provider asserts the e-mail is
    verified, and never for staff accounts (prevents account takeover through a weaker provider).
    """
    subject = subject[:255]
    identity = db.execute(
        select(UserIdentity).where(UserIdentity.provider == provider, UserIdentity.subject == subject)
    ).scalar_one_or_none()
    now = utcnow()
    clean_email: str | None = None
    if email:
        try:
            clean_email = normalize_email(email)
        except OrderError:
            clean_email = None
    if identity is not None:
        user = db.get(User, identity.user_id)
        assert user is not None
        identity.last_login_at = now
        identity.display_name = display_name[:120]
        if user.is_admin:
            raise AuthError("use_staff_login")
        if not user.is_active:
            raise AuthError("inactive")
        return user
    user = None
    if clean_email and email_verified:
        user = db.execute(select(User).where(User.email == clean_email)).scalar_one_or_none()
        if user is not None and user.is_admin:
            raise AuthError("use_staff_login")
    if user is None:
        user = User(
            email=clean_email if (clean_email and email_verified) else None,
            role="customer",
            email_verified_at=now if (clean_email and email_verified) else None,
            display_name=display_name[:80],
        )
        db.add(user)
        db.flush()
    db.add(
        UserIdentity(
            user_id=user.id,
            provider=provider,
            subject=subject,
            email=clean_email,
            email_verified=email_verified,
            display_name=display_name[:120],
            last_login_at=now,
        )
    )
    db.flush()
    return user


def verify_telegram_login(
    data: dict[str, Any], bot_token: str, max_age: int, now: float | None = None
) -> dict[str, Any]:
    """Telegram Login Widget check (core.telegram.org/widgets/login#checking-authorization).

    secret = SHA256(bot_token); hash must equal HMAC_SHA256(secret, sorted "k=v" lines without hash).
    """
    if not bot_token:
        raise AuthError("telegram_disabled")
    received = str(data.get("hash", ""))
    fields = {k: str(v) for k, v in data.items() if k != "hash" and v is not None and v != ""}
    allowed = {"id", "first_name", "last_name", "username", "photo_url", "auth_date"}
    if not received or not fields.get("id") or not set(fields) <= allowed:
        raise AuthError("telegram_invalid")
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("telegram_invalid")
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError as exc:
        raise AuthError("telegram_invalid") from exc
    if abs((now or time.time()) - auth_date) > max_age:
        raise AuthError("telegram_expired")
    return fields
