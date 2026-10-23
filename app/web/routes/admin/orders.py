"""Admin: order list/detail, manual delivery, fulfilment retry, refunds, notes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.core.money import MoneyError, to_decimal
from app.db import get_db
from app.jobs.queue import enqueue
from app.models.ops import Procurement, ReviewItem
from app.models.orders import Order, OrderEvent, Refund
from app.models.payments import PaymentAttempt
from app.models.stores import Store
from app.models.users import User
from app.services.audit import audit, order_event
from app.services.fulfillment import FulfillmentError, deliver_manual, visible_deliveries
from app.services.refunds import RefundError, mark_manual_refund_done, request_refund
from app.web.deps import csrf_form, form_str, get_store, require_admin
from app.web.templating import redirect, render

router = APIRouter()


def _order(db: Session, store: Store, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.store_id != store.id:
        raise HTTPException(404, "Order not found")
    return order


@router.get("/orders")
def orders(
    request: Request,
    q: str = "",
    status: str = "",
    page: int = 1,
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    stmt = select(Order).where(Order.store_id == store.id)
    if q:
        term = q.strip()[:254]
        stmt = stmt.where(or_(Order.order_no == term.upper(), Order.email == term.lower()))
    if status in ("unpaid", "paid", "refund_pending", "partially_refunded", "refunded"):
        stmt = stmt.where(Order.payment_status == status)
    elif status in ("awaiting_manual", "awaiting_stock", "awaiting_supplier", "needs_review", "delivered"):
        stmt = stmt.where(Order.fulfillment_status == status)
    page = max(1, page)
    rows = db.execute(stmt.order_by(Order.id.desc()).limit(50).offset((page - 1) * 50)).scalars().all()
    return render(request, "admin/orders.html", orders=rows, q=q, status=status, page=page)


@router.get("/orders/{order_id}")
def order_detail(
    request: Request,
    order_id: int,
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _order(db, store, order_id)
    attempts = (
        db.execute(
            select(PaymentAttempt).where(PaymentAttempt.order_id == order.id).order_by(PaymentAttempt.id)
        )
        .scalars()
        .all()
    )
    refunds = (
        db.execute(select(Refund).where(Refund.order_id == order.id).order_by(Refund.id)).scalars().all()
    )
    events = (
        db.execute(select(OrderEvent).where(OrderEvent.order_id == order.id).order_by(OrderEvent.id))
        .scalars()
        .all()
    )
    reviews = (
        db.execute(select(ReviewItem).where(ReviewItem.order_id == order.id).order_by(ReviewItem.id))
        .scalars()
        .all()
    )
    procurements = (
        db.execute(select(Procurement).where(Procurement.order_item_id.in_([i.id for i in order.items])))
        .scalars()
        .all()
        if order.items
        else []
    )
    show = request.query_params.get("reveal") == "1"
    deliveries = visible_deliveries(db, order) if show else []
    if show:
        audit(
            db,
            "delivery_revealed",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="order",
            target_id=order.id,
        )
        db.commit()
    return render(
        request,
        "admin/order_detail.html",
        order=order,
        attempts=attempts,
        refunds=refunds,
        events=events,
        reviews=reviews,
        procurements=procurements,
        deliveries=deliveries,
        revealed=show,
        error=request.query_params.get("error"),
    )


@router.post("/orders/{order_id}/deliver")
def order_deliver(
    request: Request,
    order_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _order(db, store, order_id)
    try:
        deliver_manual(
            db, order.id, int(form_str(form, "item_id", 20) or "0"), form_str(form, "content", 20000), user.id
        )
    except (FulfillmentError, ValueError) as exc:
        db.rollback()
        return redirect(f"/admin/orders/{order.id}?error={exc}")
    audit(
        db,
        "manual_delivery",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="order",
        target_id=order.id,
    )
    for item in db.execute(
        select(ReviewItem).where(
            ReviewItem.order_id == order.id,
            ReviewItem.status == "open",
            ReviewItem.kind.in_(("manual_delivery", "out_of_stock")),
        )
    ).scalars():
        item.status = "resolved"
        item.resolution = "delivered manually"
        item.resolved_by = user.id
    db.commit()
    return redirect(f"/admin/orders/{order.id}")


@router.post("/orders/{order_id}/retry-fulfillment")
def order_retry(
    request: Request,
    order_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _order(db, store, order_id)
    if order.payment_status != "paid":
        return redirect(f"/admin/orders/{order.id}?error=not_paid")
    for item in order.items:
        if (
            item.fulfillment_status in ("awaiting_stock", "needs_review")
            and item.fulfillment_method == "auto_card"
        ):
            item.fulfillment_status = "queued"
    import time

    enqueue(
        db,
        "fulfill_order",
        {"order_id": order.id},
        dedupe_key=f"fulfill_order:{order.id}:retry:{int(time.time())}",
    )
    audit(
        db,
        "fulfillment_retry",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="order",
        target_id=order.id,
    )
    db.commit()
    return redirect(f"/admin/orders/{order.id}")


@router.post("/orders/{order_id}/refund")
def order_refund(
    request: Request,
    order_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _order(db, store, order_id)
    attempt_raw = form_str(form, "attempt_id", 20)
    try:
        amount = to_decimal(form_str(form, "amount", 30))
        request_refund(
            db,
            order.id,
            amount,
            form_str(form, "reason", 1000),
            user.id,
            attempt_id=int(attempt_raw) if attempt_raw.isdigit() else None,
        )
    except (RefundError, MoneyError) as exc:
        db.rollback()
        return redirect(f"/admin/orders/{order.id}?error={exc}")
    audit(
        db,
        "refund_requested",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="order",
        target_id=order.id,
        details={"amount": str(amount)},
    )
    db.commit()
    return redirect(f"/admin/orders/{order.id}")


@router.post("/refunds/{refund_id}/manual-done")
def refund_manual_done(
    request: Request,
    refund_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    refund = db.get(Refund, refund_id)
    if refund is None:
        raise HTTPException(404, "Refund not found")
    order = _order(db, store, refund.order_id)
    try:
        mark_manual_refund_done(db, refund.id, form_str(form, "reference", 128), user.id)
    except RefundError as exc:
        db.rollback()
        return redirect(f"/admin/orders/{order.id}?error={exc}")
    audit(
        db,
        "manual_refund_recorded",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="refund",
        target_id=refund.id,
    )
    db.commit()
    return redirect(f"/admin/orders/{order.id}")


@router.post("/orders/{order_id}/note")
def order_note(
    request: Request,
    order_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _order(db, store, order_id)
    note = form_str(form, "note", 2000)
    if note:
        order_event(db, order.id, "note", note, actor_user_id=user.id, visible=form.get("visible") == "1")
        db.commit()
    return redirect(f"/admin/orders/{order.id}")
