"""Fulfilment after verified payment: local card codes, supplier procurement, or manual delivery."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt
from app.db import utcnow
from app.jobs.queue import enqueue
from app.models.ops import PROC_PENDING, Procurement
from app.models.orders import (
    FUL_AWAITING_MANUAL,
    FUL_AWAITING_STOCK,
    FUL_AWAITING_SUPPLIER,
    FUL_CANCELED,
    FUL_DELIVERED,
    FUL_NEEDS_REVIEW,
    FUL_PROCESSING,
    LC_COMPLETED,
    PAY_PAID,
    Delivery,
    Order,
    OrderItem,
)
from app.services.audit import order_event
from app.services.inventory import claim_codes
from app.services.review import open_review

DONE_ITEM_STATES = (FUL_DELIVERED, FUL_CANCELED)


class FulfillmentError(ValueError):
    pass


def lock_order(db: Session, order_id: int) -> Order:
    return db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one()


def can_fulfill(order: Order) -> bool:
    """Only orders whose money is verified and not being refunded may receive goods."""
    return order.payment_status == PAY_PAID


def fulfill_order(db: Session, order_id: int) -> str:
    """Idempotent: re-running never delivers twice (item counters + unique card_code_id)."""
    order = lock_order(db, order_id)
    if not can_fulfill(order):
        order_event(db, order.id, "fulfillment_skipped", f"Payment status is {order.payment_status}")
        db.commit()
        return "skipped"
    for item in order.items:
        if item.fulfillment_status in DONE_ITEM_STATES:
            continue
        method = item.fulfillment_method
        if method == "auto_card":
            _deliver_cards(db, order, item)
        elif method == "supplier":
            _start_procurement(db, order, item)
        else:
            if item.fulfillment_status != FUL_AWAITING_MANUAL:
                item.fulfillment_status = FUL_AWAITING_MANUAL
                open_review(
                    db,
                    kind="manual_delivery",
                    severity="medium",
                    order_id=order.id,
                    dedupe_key=f"manual_delivery:{item.id}",
                    summary=f"Deliver manually: {item.snapshot.get('product_title')} x{item.quantity}",
                )
                order_event(db, order.id, "awaiting_manual", "Waiting for manual delivery", visible=True)
    refresh_order_status(db, order)
    db.commit()
    return order.fulfillment_status


def _deliver_cards(db: Session, order: Order, item: OrderItem) -> None:
    need = item.quantity - item.delivered_quantity
    if need <= 0:
        item.fulfillment_status = FUL_DELIVERED
        return
    codes = claim_codes(db, item.sku_id, need, item.id)
    if not codes:
        if item.fulfillment_status != FUL_AWAITING_STOCK:
            item.fulfillment_status = FUL_AWAITING_STOCK
            open_review(
                db,
                kind="out_of_stock",
                order_id=order.id,
                dedupe_key=f"out_of_stock:{item.id}",
                summary=f"Paid order {order.order_no} needs {need} code(s) but stock is insufficient",
            )
            order_event(
                db, order.id, "awaiting_stock", "Item is being restocked; delivery delayed", visible=True
            )
        return
    for code in codes:
        db.add(
            Delivery(
                order_item_id=item.id, kind="card_code", card_code_id=code.id, content_enc=code.secret_enc
            )
        )
    item.delivered_quantity += len(codes)
    item.fulfillment_status = FUL_DELIVERED
    order_event(db, order.id, "delivered", f"{len(codes)} code(s) delivered automatically", visible=True)
    db.flush()


def _start_procurement(db: Session, order: Order, item: OrderItem) -> None:
    existing = db.execute(
        select(Procurement).where(Procurement.order_item_id == item.id)
    ).scalar_one_or_none()
    if existing is not None:
        return
    snap = item.snapshot
    if not snap.get("supplier_code") or not snap.get("supplier_sku"):
        item.fulfillment_status = FUL_NEEDS_REVIEW
        open_review(
            db,
            kind="procurement_config",
            order_id=order.id,
            dedupe_key=f"proc_cfg:{item.id}",
            summary="Supplier product is not configured; deliver manually or refund",
        )
        return
    from decimal import Decimal

    proc = Procurement(
        order_item_id=item.id,
        supplier_code=snap["supplier_code"],
        supplier_sku=snap["supplier_sku"],
        quantity=item.quantity,
        idempotency_key=f"{order.order_no}-{item.id}",
        status=PROC_PENDING,
        max_unit_cost=Decimal(snap["max_unit_cost"]) if snap.get("max_unit_cost") else None,
        currency=order.currency,
        log=[],
    )
    db.add(proc)
    db.flush()
    item.fulfillment_status = FUL_AWAITING_SUPPLIER
    order_event(db, order.id, "procuring", "Sourcing your item from our supplier", visible=True)
    enqueue(
        db,
        "procure",
        {"procurement_id": proc.id, "order_id": order.id},
        dedupe_key=f"procure:{proc.id}",
        timeout_seconds=60,
        max_attempts=5,
    )


def deliver_manual(db: Session, order_id: int, item_id: int, content: str, user_id: int) -> None:
    order = lock_order(db, order_id)
    if not can_fulfill(order):
        raise FulfillmentError("order_not_paid_or_refunding")
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise FulfillmentError("item_not_found")
    if item.fulfillment_status == FUL_DELIVERED:
        raise FulfillmentError("already_delivered")
    content = content.strip()
    if not content or len(content) > 20000:
        raise FulfillmentError("content_length")
    db.add(Delivery(order_item_id=item.id, kind="manual", content_enc=encrypt(content), delivered_by=user_id))
    item.delivered_quantity = item.quantity
    item.fulfillment_status = FUL_DELIVERED
    order_event(db, order.id, "delivered", "Delivered by staff", visible=True, actor_user_id=user_id)
    refresh_order_status(db, order)


def deliver_supplier_content(db: Session, item: OrderItem, procurement_id: int, contents: list[str]) -> None:
    for content in contents:
        db.add(
            Delivery(
                order_item_id=item.id,
                kind="supplier",
                procurement_id=procurement_id,
                content_enc=encrypt(content),
            )
        )
    item.delivered_quantity = item.quantity
    item.fulfillment_status = FUL_DELIVERED


def refresh_order_status(db: Session, order: Order) -> None:
    states = [i.fulfillment_status for i in order.items]
    previous = order.fulfillment_status
    if states and all(s == FUL_DELIVERED for s in states):
        order.fulfillment_status = FUL_DELIVERED
        order.delivered_at = order.delivered_at or utcnow()
        order.lifecycle = LC_COMPLETED
    elif any(s == FUL_NEEDS_REVIEW for s in states):
        order.fulfillment_status = FUL_NEEDS_REVIEW
    elif any(s == FUL_AWAITING_STOCK for s in states):
        order.fulfillment_status = FUL_AWAITING_STOCK
    elif any(s == FUL_AWAITING_SUPPLIER for s in states):
        order.fulfillment_status = FUL_AWAITING_SUPPLIER
    elif any(s == FUL_AWAITING_MANUAL for s in states):
        order.fulfillment_status = FUL_AWAITING_MANUAL
    elif states and all(s in DONE_ITEM_STATES for s in states):
        order.fulfillment_status = FUL_CANCELED
    else:
        order.fulfillment_status = FUL_PROCESSING
    if order.fulfillment_status == FUL_DELIVERED and previous != FUL_DELIVERED:
        enqueue(
            db,
            "send_email",
            {"template": "order_delivered", "order_id": order.id},
            dedupe_key=f"email:delivered:{order.id}",
        )


def visible_deliveries(db: Session, order: Order) -> list[dict[str, object]]:
    """Decrypted delivery contents for an *authorised* viewer of this order."""
    item_ids = [i.id for i in order.items]
    if not item_ids:
        return []
    rows = db.execute(
        select(Delivery)
        .where(Delivery.order_item_id.in_(item_ids), Delivery.revoked_at.is_(None))
        .order_by(Delivery.id)
    ).scalars()
    return [{"kind": d.kind, "content": decrypt(d.content_enc), "at": d.created_at} for d in rows]
