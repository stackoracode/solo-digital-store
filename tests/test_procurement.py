"""Upstream procurement: timeouts after success, no double purchase, price caps, rejections."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.models.ops import Procurement, ReviewItem
from app.models.orders import Delivery, Order
from app.services.procurement import check, procure
from app.suppliers.registry import MOCK_SUPPLIER
from tests.conftest import drain, make_order, make_product, mock_webhook, start


def _paid_supplier_order(db, store, cap: str | None = "2.00"):
    product = make_product(db, store, method="supplier", supplier_code="mock")
    sku = product.skus[0]
    sku.supplier_sku = "MOCK-ITEM"
    sku.max_unit_cost = Decimal(cap) if cap else None
    db.commit()
    order, _ = make_order(db, store, product)
    mock_webhook(db, start(db, order))
    return order


def _proc(db) -> Procurement:
    return db.execute(select(Procurement)).scalar_one()


def test_supplier_success_delivers(db, store):
    order = _paid_supplier_order(db, store)
    drain()
    db.expire_all()
    assert db.get(Order, order.id).fulfillment_status == "delivered"
    assert _proc(db).status == "succeeded"
    assert MOCK_SUPPLIER.place_calls == 1


def test_timeout_after_upstream_success_is_queried_not_rebought(db, store):
    MOCK_SUPPLIER.timeout_after_success = True
    order = _paid_supplier_order(db, store)
    from app.services.fulfillment import fulfill_order

    fulfill_order(db, order.id)
    proc = _proc(db)
    outcome = procure(db, proc.id)
    assert outcome.action == "check_later"
    db.expire_all()
    assert _proc(db).status == "unknown"
    assert db.get(Order, order.id).fulfillment_status == "awaiting_supplier"  # never claims "shipped"
    # Re-running the procure job (e.g. duplicate job delivery) must query, not buy again.
    assert procure(db, proc.id).action == "done"
    assert MOCK_SUPPLIER.place_calls == 1
    db.expire_all()
    assert _proc(db).status == "succeeded"
    assert db.get(Order, order.id).fulfillment_status == "delivered"
    assert db.scalar(select(func.count()).select_from(Delivery)) == 1


def test_pending_upstream_is_rechecked(db, store):
    MOCK_SUPPLIER.timeout_after_success = True
    MOCK_SUPPLIER.pending_queries = 2
    order = _paid_supplier_order(db, store)
    from app.services.fulfillment import fulfill_order

    fulfill_order(db, order.id)
    proc = _proc(db)
    procure(db, proc.id)
    assert check(db, proc.id).action == "check_later"
    assert check(db, proc.id).action == "check_later"
    assert check(db, proc.id).action == "done"
    assert MOCK_SUPPLIER.place_calls == 1
    db.expire_all()
    assert db.get(Order, order.id).fulfillment_status == "delivered"


def test_price_above_cap_goes_to_review_without_buying(db, store):
    MOCK_SUPPLIER.unit_cost = Decimal("3.50")
    order = _paid_supplier_order(db, store, cap="2.00")
    drain()
    db.expire_all()
    assert _proc(db).status == "needs_review"
    assert MOCK_SUPPLIER.place_calls == 0
    assert db.get(Order, order.id).fulfillment_status == "needs_review"
    assert db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.kind == "price_cap")) == 1


def test_supplier_rejection_creates_review(db, store):
    MOCK_SUPPLIER.reject_next = True
    order = _paid_supplier_order(db, store)
    drain()
    db.expire_all()
    assert _proc(db).status == "failed"
    assert db.get(Order, order.id).fulfillment_status == "needs_review"
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0


def test_procurement_canceled_if_refunded_before_submit(db, store):
    from app.services.fulfillment import fulfill_order
    from app.services.refunds import request_refund

    order = _paid_supplier_order(db, store)
    fulfill_order(db, order.id)
    request_refund(db, order.id, Decimal("10.00"), "refund", user_id=None)
    db.commit()
    proc = _proc(db)
    procure(db, proc.id)
    assert _proc(db).status == "canceled"
    assert MOCK_SUPPLIER.place_calls == 0
