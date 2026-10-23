"""Test fixtures. Requires PostgreSQL (CI provides a service container); set TEST_DATABASE_URL to override."""

from __future__ import annotations

import os

os.environ.setdefault("ENV", "test")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://store:store@localhost:5432/store_test"
)
os.environ.setdefault("PRIMARY_HOSTS", "testserver,localhost")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-0123456789")
os.environ.setdefault("FINGERPRINT_KEY", "test-fingerprint-key-not-for-production-0123")
os.environ.setdefault("MOCK_PAYMENTS_ENABLED", "true")
os.environ.setdefault("ADMIN_REQUIRE_MFA", "true")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("LOCAL_MEDIA_DIR", "/tmp/solostore-test-media")

from decimal import Decimal  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401
from app.core.ratelimit import get_limiter  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.suppliers.registry import MOCK_SUPPLIER  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    yield
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    get_limiter().reset()
    MOCK_SUPPLIER.reset()
    from app.core.middleware import invalidate_domain_cache
    from app.web.deps import clear_store_cache

    clear_store_cache()
    invalidate_domain_cache()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def store(db):
    from app.cli import bootstrap

    return bootstrap(db)


@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://testserver") as c:
        yield c


def make_product(db, store, *, method: str = "auto_card", price: str = "10.00", codes: int = 0, **extra: Any):
    from app.services.catalog import quick_create_product
    from app.services.inventory import import_codes

    product = quick_create_product(
        db,
        store_id=store.id,
        title=extra.pop("title", f"Item {method}"),
        price=price,
        description="test",
        fulfillment_method=method,
    )
    for key, value in extra.items():
        setattr(product, key, value)
    if codes:
        import_codes(db, product.skus[0], "\n".join(f"CODE-{product.id}-{i}" for i in range(codes)))
    db.commit()
    db.refresh(product)
    return product


class FormStub(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def make_order(
    db,
    store,
    product,
    *,
    quantity: int = 1,
    email: str = "buyer@example.com",
    form=None,
    user=None,
    agent_code=None,
):
    from app.services.orders import create_order

    order, token = create_order(
        db,
        store_id=store.id,
        product=product,
        sku=product.skus[0],
        quantity=quantity,
        email=email,
        form=FormStub(form or {}),
        locale="en-US",
        client_ip="203.0.113.5",
        user=user,
        agent_code=agent_code,
    )
    db.commit()
    return order, token


def start(db, order, channel: str = "mock"):
    from app.services.payments import start_payment

    return start_payment(db, order, channel)


def mock_webhook(
    db, attempt, *, outcome: str = "payment.succeeded", amount: str | None = None, event_id: str = "evt_1"
):
    from app.payments import mock
    from app.services.payments import handle_webhook

    body = mock.build_event(
        attempt.attempt_no, outcome, amount or str(attempt.pay_amount), attempt.pay_currency, event_id
    )
    return handle_webhook(db, attempt.channel_code, {mock.SIG_HEADER: mock.sign(body)}, body, "198.51.100.1")


def drain():
    from app.jobs.worker import run_until_empty

    return run_until_empty()


def enable_channel(db, code: str, **fields: Any):
    from sqlalchemy import select

    from app.models.payments import PaymentChannel

    ch = db.execute(select(PaymentChannel).where(PaymentChannel.code == code)).scalar_one()
    ch.enabled = True
    for k, v in fields.items():
        setattr(ch, k, v)
    db.commit()
    return ch


D = Decimal
