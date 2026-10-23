"""Card codes: encryption, de-duplication, concurrent claiming, refunds vs fulfilment races."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.inventory import CardCode
from app.models.ops import ReviewItem
from app.models.orders import Delivery, Order
from app.services.fulfillment import fulfill_order
from app.services.inventory import import_codes
from app.services.refunds import request_refund
from tests.conftest import D, drain, make_order, make_product, mock_webhook, start


def test_codes_are_encrypted_and_deduplicated(db, store):
    product = make_product(db, store)
    sku = product.skus[0]
    r1 = import_codes(db, sku, "AAA-111\nBBB-222\nAAA-111\n\n")
    r2 = import_codes(db, sku, "BBB-222\nCCC-333")
    db.commit()
    assert (r1.imported, r1.duplicates) == (2, 1)
    assert (r2.imported, r2.duplicates) == (1, 1)
    secrets = db.execute(select(CardCode.secret_enc)).scalars().all()
    assert len(secrets) == 3
    assert all("AAA" not in s and "BBB" not in s for s in secrets)


@pytest.mark.concurrency
def test_two_orders_compete_for_the_same_last_code(db, store):
    product = make_product(db, store, codes=1)
    orders = []
    for i in range(2):
        order, _ = make_order(db, store, product, email=f"b{i}@example.com")
        attempt = start(db, order)
        mock_webhook(db, attempt, event_id=f"e{i}")
        orders.append(order.id)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(order_id: int) -> None:
        s = SessionLocal()
        try:
            barrier.wait()
            results.append(fulfill_order(s, order_id))
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(oid,)) for oid in orders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["awaiting_stock", "delivered"]
    assert db.scalar(select(func.count()).select_from(Delivery)) == 1
    assert (
        db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.kind == "out_of_stock")) == 1
    )


def test_fulfilment_is_idempotent(db, store):
    product = make_product(db, store, codes=5)
    order, _ = make_order(db, store, product, quantity=2)
    mock_webhook(db, start(db, order))
    assert fulfill_order(db, order.id) == "delivered"
    assert fulfill_order(db, order.id) == "delivered"
    assert db.scalar(select(func.count()).select_from(Delivery)) == 2
    assert db.scalar(select(func.count()).select_from(CardCode).where(CardCode.status == "sold")) == 2


def test_no_shipment_after_refund_requested(db, store):
    product = make_product(db, store, codes=2)
    order, _ = make_order(db, store, product)
    mock_webhook(db, start(db, order))
    # Refund lands before the fulfilment job runs.
    request_refund(db, order.id, D("10.00"), "customer changed mind", user_id=None)
    db.commit()
    drain()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.payment_status == "refunded"
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0
    assert db.scalar(select(func.count()).select_from(CardCode).where(CardCode.status == "available")) == 2
    # Re-running fulfilment after the refund still ships nothing.
    assert fulfill_order(db, order.id) == "skipped"
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0


def test_partial_refund_and_double_refund_guard(db, store):
    from app.services.refunds import RefundError

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    mock_webhook(db, start(db, order))
    drain()
    request_refund(db, order.id, D("4.00"), "partial", user_id=None)
    db.commit()
    with pytest.raises(RefundError):
        request_refund(db, order.id, D("1.00"), "concurrent", user_id=None)
    db.rollback()
    drain()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.payment_status == "partially_refunded"
    assert order.refunded_total == D("4.00")
    with pytest.raises(RefundError):
        request_refund(db, order.id, D("6.01"), "too much", user_id=None)


def test_manual_delivery_flow(db, store):
    from app.services.fulfillment import deliver_manual, visible_deliveries

    product = make_product(db, store, method="manual")
    order, _ = make_order(db, store, product)
    mock_webhook(db, start(db, order))
    drain()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.fulfillment_status == "awaiting_manual"
    deliver_manual(
        db, order.id, order.items[0].id, "Here is your design: https://example.com/file", user_id=None
    )
    db.commit()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.fulfillment_status == "delivered"
    assert visible_deliveries(db, order)[0]["content"].startswith("Here is your design")
