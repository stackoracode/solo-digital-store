"""Upstream procurement state machine.

Rules: persist intent (status=submitted) BEFORE calling the supplier; a timeout means "unknown" and is
resolved by querying with the same idempotency key, never by blindly re-ordering; price caps are checked
before submitting; anything ambiguous becomes a review item and the customer sees "delayed", not "shipped".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import encrypt
from app.db import utcnow
from app.models.ops import (
    PROC_CANCELED,
    PROC_FAILED,
    PROC_NEEDS_REVIEW,
    PROC_PENDING,
    PROC_SUBMITTED,
    PROC_SUCCEEDED,
    PROC_UNKNOWN,
    Procurement,
)
from app.models.orders import FUL_NEEDS_REVIEW, PAY_PAID, Delivery, Order, OrderItem
from app.services.audit import order_event
from app.services.fulfillment import deliver_supplier_content, refresh_order_status
from app.services.review import open_review
from app.suppliers.base import SupplierOrder, SupplierRejected, SupplierTimeout, SupplierUnavailable
from app.suppliers.registry import get_supplier

log = logging.getLogger(__name__)
MAX_QUERIES = 20
MAX_SUBMITS = 3
FINAL = (PROC_SUCCEEDED, PROC_FAILED, PROC_NEEDS_REVIEW, PROC_CANCELED)


@dataclass
class Outcome:
    action: str  # done | check_later | retry
    delay: float = 0


def _lock(db: Session, procurement_id: int) -> tuple[Procurement, OrderItem, Order]:
    proc = db.get(Procurement, procurement_id)
    if proc is None:
        raise LookupError(procurement_id)
    item = db.get(OrderItem, proc.order_item_id)
    assert item is not None
    order = db.execute(select(Order).where(Order.id == item.order_id).with_for_update()).scalar_one()
    proc = db.execute(
        select(Procurement).where(Procurement.id == procurement_id).with_for_update()
    ).scalar_one()
    db.refresh(item)
    return proc, item, order


def _log(proc: Procurement, event: str, **data: object) -> None:
    entry = {"at": utcnow().isoformat(), "event": event}
    entry.update({k: str(v)[:300] for k, v in data.items()})
    proc.log = [*(proc.log or []), entry][-50:]


def _to_review(
    db: Session, proc: Procurement, item: OrderItem, order: Order, kind: str, summary: str
) -> None:
    proc.status = PROC_NEEDS_REVIEW
    item.fulfillment_status = FUL_NEEDS_REVIEW
    open_review(
        db,
        kind=kind,
        order_id=order.id,
        procurement_id=proc.id,
        dedupe_key=f"{kind}:{proc.id}",
        summary=summary,
    )
    order_event(
        db, order.id, "procurement_review", "Delivery is delayed; our team is handling it", visible=True
    )
    refresh_order_status(db, order)


def procure(db: Session, procurement_id: int) -> Outcome:
    proc, item, order = _lock(db, procurement_id)
    if proc.status in FINAL:
        db.commit()
        return Outcome("done")
    if proc.status in (PROC_SUBMITTED, PROC_UNKNOWN):
        db.commit()
        return check(db, procurement_id)  # crashed mid-submit earlier: query, do not re-send
    if order.payment_status != PAY_PAID:
        proc.status = PROC_CANCELED
        _log(proc, "canceled_before_submit", payment_status=order.payment_status)
        db.commit()
        return Outcome("done")
    try:
        adapter = get_supplier(proc.supplier_code)
    except KeyError as exc:
        _to_review(db, proc, item, order, "procurement_config", f"Supplier adapter missing: {exc}")
        db.commit()
        return Outcome("done")

    quote = adapter.quote(proc.supplier_sku, proc.quantity)  # raises -> job retry, nothing was sent
    proc.quoted_unit_cost = quote.unit_cost
    _log(proc, "quote", unit_cost=quote.unit_cost, available=quote.available)
    if not quote.available:
        _to_review(db, proc, item, order, "supplier_out_of_stock", "Supplier reports out of stock")
        db.commit()
        return Outcome("done")
    if proc.max_unit_cost is not None and quote.unit_cost > proc.max_unit_cost:
        _to_review(
            db,
            proc,
            item,
            order,
            "price_cap",
            f"Supplier price {quote.unit_cost} exceeds cap {proc.max_unit_cost}",
        )
        db.commit()
        return Outcome("done")

    proc.status = PROC_SUBMITTED
    proc.submit_count += 1
    _log(proc, "submit", key=proc.idempotency_key)
    db.commit()  # intent is durable before any money can move upstream

    try:
        result = adapter.place_order(
            proc.supplier_sku, proc.quantity, proc.idempotency_key, proc.max_unit_cost
        )
    except SupplierUnavailable as exc:
        proc, item, order = _lock(db, procurement_id)
        proc.status = PROC_PENDING
        proc.last_error = str(exc)[:500]
        _log(proc, "unavailable", error=exc)
        db.commit()
        return Outcome("retry", 60)
    except SupplierRejected as exc:
        proc, item, order = _lock(db, procurement_id)
        proc.status = PROC_FAILED
        proc.last_error = str(exc)[:500]
        _log(proc, "rejected", error=exc)
        _to_review(db, proc, item, order, "procurement_failed", f"Supplier rejected order: {exc}")
        proc.status = PROC_FAILED
        db.commit()
        return Outcome("done")
    except (SupplierTimeout, Exception) as exc:  # noqa: BLE001 - any ambiguity means "unknown", never resend
        proc, item, order = _lock(db, procurement_id)
        proc.status = PROC_UNKNOWN
        proc.last_error = f"{exc.__class__.__name__}: {exc}"[:500]
        _log(proc, "unknown", error=exc)
        order_event(db, order.id, "procurement_pending", "Confirming with supplier", visible=True)
        db.commit()
        return Outcome("check_later", 30)

    proc, item, order = _lock(db, procurement_id)
    outcome = _apply(db, proc, item, order, result)
    db.commit()
    return outcome


def check(db: Session, procurement_id: int) -> Outcome:
    proc, item, order = _lock(db, procurement_id)
    if proc.status not in (PROC_SUBMITTED, PROC_UNKNOWN):
        db.commit()
        return Outcome("done")
    adapter = get_supplier(proc.supplier_code)
    db.commit()  # don't hold row locks across the network call
    result = adapter.query_order(proc.idempotency_key)
    proc, item, order = _lock(db, procurement_id)
    if proc.status not in (PROC_SUBMITTED, PROC_UNKNOWN):
        db.commit()
        return Outcome("done")
    proc.query_count += 1
    _log(proc, "query", status=result.status)
    if result.status == "not_found":
        if adapter.idempotent_submit and proc.submit_count < MAX_SUBMITS:
            proc.status = PROC_PENDING  # upstream dedupes on our key, re-sending cannot double-buy
            db.commit()
            return procure(db, procurement_id)
        _to_review(
            db,
            proc,
            item,
            order,
            "procurement_unknown",
            "Supplier has no record of the order and does not support idempotent retry",
        )
        db.commit()
        return Outcome("done")
    if result.status == "pending":
        if proc.query_count >= MAX_QUERIES:
            _to_review(
                db, proc, item, order, "procurement_timeout", "Supplier still pending after max checks"
            )
            db.commit()
            return Outcome("done")
        db.commit()
        return Outcome("check_later", min(600, 30 * proc.query_count))
    outcome = _apply(db, proc, item, order, result)
    db.commit()
    return outcome


def _apply(db: Session, proc: Procurement, item: OrderItem, order: Order, result: SupplierOrder) -> Outcome:
    if result.status == "failed":
        proc.status = PROC_FAILED
        _to_review(db, proc, item, order, "procurement_failed", f"Supplier failed: {result.message}")
        proc.status = PROC_FAILED
        return Outcome("done")
    if result.status != "succeeded":
        _to_review(
            db, proc, item, order, "procurement_unknown", f"Unexpected supplier status {result.status}"
        )
        return Outcome("done")
    proc.status = PROC_SUCCEEDED
    proc.supplier_order_no = result.supplier_order_no
    proc.actual_cost = result.cost
    _log(proc, "succeeded", supplier_order_no=result.supplier_order_no, cost=result.cost)
    contents = result.contents or [f"Completed by supplier (ref {result.supplier_order_no})"]
    if result.cost is not None:
        order.cost_total = order.cost_total + result.cost
        if proc.max_unit_cost is not None and result.cost > proc.max_unit_cost * proc.quantity:
            open_review(
                db,
                kind="cost_above_cap",
                severity="medium",
                order_id=order.id,
                procurement_id=proc.id,
                dedupe_key=f"cost_above_cap:{proc.id}",
                summary=f"Final cost {result.cost} above cap",
            )
    if order.payment_status != PAY_PAID:
        # Refund raced with procurement: keep goods out of the customer's reach, escalate.
        for content in contents:
            db.add(
                Delivery(
                    order_item_id=item.id,
                    kind="supplier",
                    procurement_id=proc.id,
                    content_enc=encrypt(content),
                    revoked_at=utcnow(),
                )
            )
        open_review(
            db,
            kind="procured_after_refund",
            order_id=order.id,
            procurement_id=proc.id,
            dedupe_key=f"procured_after_refund:{proc.id}",
            summary="Supplier delivered after the order entered refund; recover or resell the item",
        )
        return Outcome("done")
    if len(result.contents) not in (0, item.quantity):
        _to_review(
            db,
            proc,
            item,
            order,
            "procurement_quantity",
            f"Supplier returned {len(result.contents)} items for quantity {item.quantity}",
        )
        proc.status = PROC_SUCCEEDED
        return Outcome("done")
    deliver_supplier_content(db, item, proc.id, contents)
    order_event(db, order.id, "delivered", "Delivered via supplier", visible=True)
    refresh_order_status(db, order)
    return Outcome("done")


def admin_take_over(db: Session, procurement_id: int, user_id: int, note: str) -> None:
    proc, item, order = _lock(db, procurement_id)
    proc.status = PROC_NEEDS_REVIEW
    proc.taken_over_by = user_id
    _log(proc, "taken_over", note=note)
    item.fulfillment_status = FUL_NEEDS_REVIEW
    refresh_order_status(db, order)
