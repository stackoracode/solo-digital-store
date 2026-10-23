"""Passwordless e-mail codes, OAuth identity linking rules, Telegram verification, agent commissions."""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security import decrypt
from app.models.ops import Job
from app.models.stores import Agent, Commission
from app.models.users import User
from app.services.auth import (
    AuthError,
    login_with_identity,
    request_email_code,
    verify_email_code,
    verify_telegram_login,
)
from tests.conftest import drain, make_order, make_product, mock_webhook, start


def _last_code(db) -> str:
    job = db.execute(select(Job).where(Job.kind == "send_email").order_by(Job.id.desc())).scalars().first()
    return decrypt(job.payload["code_enc"])


def test_email_code_login_creates_verified_customer(db, store):
    request_email_code(db, "New@Example.com", ip="1.1.1.1", locale="en-US")
    db.commit()
    code = _last_code(db)
    with pytest.raises(AuthError):
        verify_email_code(db, "new@example.com", "000000" if code != "000000" else "111111")
    db.commit()
    user = verify_email_code(db, "new@example.com", code)
    db.commit()
    assert user.email == "new@example.com" and user.email_verified_at is not None and user.role == "customer"
    with pytest.raises(AuthError):  # single use
        verify_email_code(db, "new@example.com", code)


def test_email_code_attempts_are_capped(db, store):
    request_email_code(db, "cap@example.com", ip="1.1.1.1", locale="en-US")
    db.commit()
    code = _last_code(db)
    for _ in range(5):
        with pytest.raises(AuthError):
            verify_email_code(db, "cap@example.com", "999999" if code != "999999" else "888888")
        db.commit()
    with pytest.raises(AuthError):
        verify_email_code(db, "cap@example.com", code)  # locked after too many attempts


def test_email_code_cannot_open_staff_account(db, store):
    from app.cli import create_admin

    create_admin(db, "staff@example.com", "correct horse battery staple")
    request_email_code(db, "staff@example.com", ip="1.1.1.1", locale="en-US")
    db.commit()
    with pytest.raises(AuthError):
        verify_email_code(db, "staff@example.com", _last_code(db))


def test_oauth_links_only_verified_emails(db, store):
    existing = User(email="alice@example.com", role="customer")
    db.add(existing)
    db.commit()
    unverified = login_with_identity(
        db, provider="oidc:x", subject="1", email="alice@example.com", email_verified=False, display_name="A"
    )
    assert unverified.id != existing.id and unverified.email is None
    verified = login_with_identity(
        db, provider="google", subject="g-1", email="alice@example.com", email_verified=True, display_name="A"
    )
    assert verified.id == existing.id
    again = login_with_identity(
        db, provider="google", subject="g-1", email=None, email_verified=False, display_name="A"
    )
    assert again.id == existing.id


def test_oauth_never_logs_into_staff(db, store):
    from app.cli import create_admin

    create_admin(db, "root@example.com", "correct horse battery staple")
    with pytest.raises(AuthError):
        login_with_identity(
            db,
            provider="google",
            subject="g-2",
            email="root@example.com",
            email_verified=True,
            display_name="R",
        )


def _telegram_payload(token: str, **fields: str) -> dict:
    data = {
        "id": "424242",
        "first_name": "Tg",
        "username": "tguser",
        "auth_date": str(int(time.time())),
        **fields,
    }
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    return data


def test_telegram_login_verification():
    token = "123456:TEST-TOKEN"
    good = _telegram_payload(token)
    assert verify_telegram_login(good, token, 600)["id"] == "424242"
    tampered = dict(good, id="1")
    with pytest.raises(AuthError):
        verify_telegram_login(tampered, token, 600)
    old = _telegram_payload(token, auth_date=str(int(time.time()) - 3600))
    with pytest.raises(AuthError):
        verify_telegram_login(old, token, 600)


def _active_agent(db, email="agent@example.com", rate="0.10"):
    user = User(email=email, role="customer")
    db.add(user)
    db.flush()
    agent = Agent(user_id=user.id, code="AGENT001", status="active", commission_rate=Decimal(rate))
    db.add(agent)
    db.commit()
    return agent


def test_commission_created_on_payment_and_reversed_on_refund(db, store):
    from app.services.refunds import request_refund

    agent = _active_agent(db)
    product = make_product(db, store, codes=1, price="20.00")
    order, _ = make_order(db, store, product, agent_code=agent.code)
    assert order.agent_id == agent.id
    mock_webhook(db, start(db, order))
    commission = db.execute(select(Commission)).scalar_one()
    assert commission.amount == Decimal("2.00") and commission.status == "pending"
    drain()
    request_refund(db, order.id, Decimal("20.00"), "refund", user_id=None)
    db.commit()
    drain()
    db.expire_all()
    commission = db.execute(select(Commission)).scalar_one()
    assert commission.status == "reversed" and commission.amount == Decimal("0")


def test_partial_refund_reduces_commission(db, store):
    from app.services.refunds import request_refund

    agent = _active_agent(db)
    product = make_product(db, store, codes=1, price="20.00")
    order, _ = make_order(db, store, product, agent_code=agent.code)
    mock_webhook(db, start(db, order))
    drain()
    request_refund(db, order.id, Decimal("5.00"), "partial", user_id=None)
    db.commit()
    drain()
    db.expire_all()
    assert db.execute(select(Commission)).scalar_one().amount == Decimal("1.50")


def test_self_referral_and_inactive_agents_earn_nothing(db, store):
    agent = _active_agent(db, email="self@example.com")
    product = make_product(db, store, codes=2)
    order, _ = make_order(db, store, product, email="self@example.com", agent_code=agent.code)
    assert order.agent_id is None
    agent.status = "suspended"
    db.commit()
    order2, _ = make_order(db, store, product, email="x@example.com", agent_code=agent.code)
    assert order2.agent_id is None
