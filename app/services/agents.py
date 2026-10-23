"""Agent distribution (Phase 1): referral attribution and single-level commissions, admin-approved."""

from __future__ import annotations

import secrets
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import q
from app.db import utcnow
from app.models.orders import Order
from app.models.stores import Agent, Commission
from app.models.users import User

MAX_RATE = Decimal("0.5")


class AgentError(ValueError):
    pass


def new_agent_code() -> str:
    return "A" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(7))


def apply_agent(db: Session, user: User, note: str) -> Agent:
    existing = db.execute(select(Agent).where(Agent.user_id == user.id)).scalar_one_or_none()
    if existing is not None:
        raise AgentError("already_applied")
    agent = Agent(
        user_id=user.id, code=new_agent_code(), status="applied", application_note=note.strip()[:2000]
    )
    db.add(agent)
    db.flush()
    return agent


def set_agent_status(
    db: Session, agent: Agent, status: str, rate: Decimal | None = None, note: str = ""
) -> None:
    if status not in ("active", "rejected", "suspended"):
        raise AgentError("invalid_status")
    if rate is not None:
        if not Decimal("0") <= rate <= MAX_RATE:
            raise AgentError("invalid_rate")
        agent.commission_rate = rate
    agent.status = status
    agent.admin_note = note[:2000]
    if status == "active" and agent.approved_at is None:
        agent.approved_at = utcnow()


def create_commission(db: Session, order: Order) -> Commission | None:
    """Called inside apply_payment_success (same transaction). One commission per order (unique)."""
    if order.agent_id is None:
        return None
    agent = db.get(Agent, order.agent_id)
    if agent is None or agent.status != "active":
        return None
    if db.scalar(select(Commission.id).where(Commission.order_id == order.id)):
        return None
    commission = Commission(
        order_id=order.id,
        agent_id=agent.id,
        base_amount=order.total,
        rate=agent.commission_rate,
        amount=q(order.total * agent.commission_rate, order.currency),
        currency=order.currency,
        status="pending",
    )
    db.add(commission)
    return commission


def adjust_commission_for_refund(db: Session, order: Order) -> None:
    commission = db.execute(
        select(Commission).where(Commission.order_id == order.id).with_for_update()
    ).scalar_one_or_none()
    if commission is None or commission.status == "reversed":
        return
    net = max(Decimal("0"), order.total - order.refunded_total)
    new_amount = q(net * commission.rate, commission.currency)
    if commission.status == "paid":
        # Already paid out: record a clawback amount for the next settlement instead of rewriting history.
        commission.reversed_amount = commission.amount - new_amount
        commission.note = (commission.note + f"\nclawback {commission.reversed_amount} after refund").strip()
        return
    commission.reversed_amount = commission.amount - new_amount
    commission.amount = new_amount
    if new_amount == 0:
        commission.status = "reversed"


def agent_stats(db: Session, agent: Agent) -> dict[str, object]:
    rows = db.execute(
        select(Commission.status, func.count(), func.coalesce(func.sum(Commission.amount), 0))
        .where(Commission.agent_id == agent.id)
        .group_by(Commission.status)
    ).all()
    by_status = {status: {"count": int(n), "amount": Decimal(total)} for status, n, total in rows}
    orders = db.scalar(select(func.count()).select_from(Order).where(Order.agent_id == agent.id)) or 0
    paid_orders = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.agent_id == agent.id, Order.paid_at.is_not(None))
        )
        or 0
    )
    return {"orders": orders, "paid_orders": paid_orders, "by_status": by_status}
