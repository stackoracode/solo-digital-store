"""Payment state machine: idempotent callbacks, duplicates, amounts, failures after payment."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.inventory import CardCode
from app.models.ops import Job, ReviewItem
from app.models.orders import Delivery, Order
from app.models.payments import PaymentAttempt, PaymentEvent
from app.services.payments import PaymentError
from tests.conftest import D, drain, make_order, make_product, mock_webhook, start


def _count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def test_duplicate_payment_callbacks_deliver_once(db, store):
    product = make_product(db, store, codes=3)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    assert mock_webhook(db, attempt, event_id="evt_same") == (200, "paid")
    # Same event delivered again (provider retry) and a different event id for the same payment.
    assert mock_webhook(db, attempt, event_id="evt_same") == (200, "duplicate")
    assert mock_webhook(db, attempt, event_id="evt_other")[1] == "already_applied"
    assert _count(db, Job, Job.kind == "fulfill_order") == 1
    drain()
    drain()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.payment_status == "paid" and order.fulfillment_status == "delivered"
    assert _count(db, Delivery) == 1
    assert _count(db, CardCode, CardCode.status == "sold") == 1
    assert _count(db, PaymentEvent, PaymentEvent.provider == "mock") == 2


def test_invalid_signature_is_rejected_and_logged(db, store):
    from app.payments import mock
    from app.services.payments import handle_webhook

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    body = mock.build_event(attempt.attempt_no, "payment.succeeded", "10.00", "USD", "evt_forged")
    status, _ = handle_webhook(db, "mock", {mock.SIG_HEADER: "0" * 64}, body, "203.0.113.9")
    assert status == 401
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    assert _count(db, PaymentEvent, PaymentEvent.signature_valid.is_(False)) == 1


def test_success_page_alone_never_marks_paid(client, db, store):
    product = make_product(db, store, codes=1)
    order, token = make_order(db, store, product)
    start(db, order)
    resp = client.get(f"/o/{order.order_no}/return?t={token}")
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_repeated_payment_initiation_reuses_attempt(db, store):
    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    a1 = start(db, order)
    a2 = start(db, order)
    assert a1.id == a2.id
    assert _count(db, PaymentAttempt, PaymentAttempt.order_id == order.id) == 1


def test_duplicate_payment_via_second_attempt_goes_to_review(db, store):
    product = make_product(db, store, codes=2)
    order, _ = make_order(db, store, product)
    first = start(db, order)
    # Simulate a second channel attempt (e.g. user paid twice from two tabs) by expiring the first.
    first.status = "expired"
    db.commit()
    second = start(db, order)
    assert second.id != first.id
    assert mock_webhook(db, second, event_id="e2")[1] == "paid"
    first = db.get(PaymentAttempt, first.id)
    first.status = "pending"
    db.commit()
    assert mock_webhook(db, first, event_id="e1")[1] == "duplicate_payment"
    drain()
    assert _count(db, Delivery) == 1
    assert _count(db, ReviewItem, ReviewItem.kind == "duplicate_payment") == 1


def test_underpaid_is_not_fulfilled(db, store):
    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    assert mock_webhook(db, attempt, amount="9.99")[1] == "underpaid"
    drain()
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    assert _count(db, Delivery) == 0
    assert _count(db, ReviewItem, ReviewItem.kind == "underpaid") == 1


def test_overpaid_is_paid_and_flagged(db, store):
    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    assert mock_webhook(db, attempt, amount="12.00")[1] == "paid"
    assert _count(db, ReviewItem, ReviewItem.kind == "overpaid") == 1


def test_currency_mismatch_goes_to_review(db, store):
    from app.services.payments import apply_payment_success

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    result = apply_payment_success(
        db, attempt.id, amount=D("10.00"), currency="EUR", provider_payment_id=None, source="test"
    )
    db.commit()
    assert result == "currency_mismatch"
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_late_payment_for_expired_order_needs_review(db, store):
    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    order = db.get(Order, order.id)
    order.lifecycle = "expired"
    db.commit()
    assert mock_webhook(db, attempt)[1] == "late_review"
    drain()
    assert _count(db, Delivery) == 0


def test_database_failure_after_payment_is_retried_safely(db, store, monkeypatch):
    """If processing crashes mid-transaction, nothing is half-applied and the provider retry succeeds once."""
    import app.services.payments as payments

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    attempt = start(db, order)
    real_enqueue = payments.enqueue
    calls = {"n": 0}

    def failing_enqueue(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated database failure")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(payments, "enqueue", failing_enqueue)
    with pytest.raises(RuntimeError):
        mock_webhook(db, attempt, event_id="evt_crash")
    db.rollback()
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    assert _count(db, PaymentEvent, PaymentEvent.event_id == "evt_crash") == 0
    assert mock_webhook(db, attempt, event_id="evt_crash")[1] == "paid"
    drain()
    assert _count(db, Delivery) == 1


def test_mock_channel_cannot_be_used_when_disabled(db, store, monkeypatch):
    from app.config import get_settings

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    monkeypatch.setattr(get_settings(), "mock_payments_enabled", False)
    with pytest.raises(PaymentError):
        start(db, order)
