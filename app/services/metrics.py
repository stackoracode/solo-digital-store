"""Lean business metrics for a solo operator. All money math in Decimal."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.security import keyed_hash
from app.db import utcnow
from app.models.ops import Procurement, ReviewItem, VisitorDaily
from app.models.orders import Order, Refund
from app.models.payments import PaymentAttempt
from app.models.stores import Commission


def record_visit(db: Session, ip: str, user_agent: str) -> None:
    today = utcnow().date()
    digest = keyed_hash(f"{today}:{ip}:{user_agent[:200]}", "visitor")
    db.execute(pg_insert(VisitorDaily).values(day=today, visitor_hash=digest).on_conflict_do_nothing())
    db.commit()


def dashboard(db: Session, store_id: int, days: int = 30) -> dict[str, object]:
    since = utcnow() - timedelta(days=days)
    since_day: date = since.date()
    visitors = (
        db.scalar(select(func.count()).select_from(VisitorDaily).where(VisitorDaily.day >= since_day)) or 0
    )
    orders_q = select(Order).where(Order.store_id == store_id, Order.created_at >= since)
    orders = db.scalar(select(func.count()).select_from(orders_q.subquery())) or 0
    paid = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.fee_total), 0),
            func.coalesce(func.sum(Order.cost_total), 0),
            func.coalesce(func.sum(Order.refunded_total), 0),
        ).where(Order.store_id == store_id, Order.paid_at.is_not(None), Order.paid_at >= since)
    ).one()
    paid_count, revenue, fees, costs, refunded = (
        int(paid[0]),
        Decimal(paid[1]),
        Decimal(paid[2]),
        Decimal(paid[3]),
        Decimal(paid[4]),
    )
    attempts = (
        db.scalar(select(func.count()).select_from(PaymentAttempt).where(PaymentAttempt.created_at >= since))
        or 0
    )
    attempts_ok = (
        db.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.created_at >= since, PaymentAttempt.status == "succeeded")
        )
        or 0
    )
    delivered = db.execute(
        select(func.count(), func.avg(func.extract("epoch", Order.delivered_at - Order.paid_at))).where(
            Order.store_id == store_id, Order.delivered_at.is_not(None), Order.paid_at >= since
        )
    ).one()
    refunds = (
        db.scalar(
            select(func.count())
            .select_from(Refund)
            .where(Refund.created_at >= since, Refund.status == "succeeded")
        )
        or 0
    )
    commissions = db.scalar(
        select(func.coalesce(func.sum(Commission.amount), 0)).where(
            Commission.created_at >= since, Commission.status != "reversed"
        )
    ) or Decimal(0)
    open_reviews = (
        db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")) or 0
    )
    open_procurements = (
        db.scalar(
            select(func.count())
            .select_from(Procurement)
            .where(Procurement.status.in_(("submitted", "unknown")))
        )
        or 0
    )
    gross_contribution = revenue - refunded - fees - costs - Decimal(commissions)
    return {
        "days": days,
        "visitors": visitors,
        "orders": orders,
        "paid_orders": paid_count,
        "conversion": (Decimal(paid_count) / Decimal(visitors) * 100).quantize(Decimal("0.01"))
        if visitors
        else None,
        "payment_success_rate": (Decimal(attempts_ok) / Decimal(attempts) * 100).quantize(Decimal("0.1"))
        if attempts
        else None,
        "delivery_rate": (Decimal(int(delivered[0])) / Decimal(paid_count) * 100).quantize(Decimal("0.1"))
        if paid_count
        else None,
        "avg_delivery_seconds": int(delivered[1]) if delivered[1] is not None else None,
        "revenue": revenue,
        "refunded": refunded,
        "refund_count": refunds,
        "fees": fees,
        "procurement_costs": costs,
        "commissions": Decimal(commissions),
        "gross_contribution": gross_contribution,
        "open_reviews": open_reviews,
        "open_procurements": open_procurements,
    }
