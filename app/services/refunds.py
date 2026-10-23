"""Refunds. Requesting a refund immediately blocks further fulfilment (order row lock + status check)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import q
from app.db import utcnow
from app.jobs.queue import enqueue
from app.models.orders import (
    FUL_CANCELED,
    FUL_DELIVERED,
    PAY_PAID,
    PAY_PARTIALLY_REFUNDED,
    PAY_REFUND_PENDING,
    PAY_REFUNDED,
    Order,
    Refund,
)
from app.models.payments import ATT_SUCCEEDED, PaymentAttempt, PaymentChannel
from app.payments.base import ProviderError
from app.payments.registry import get_provider
from app.services.audit import order_event
from app.services.orders import new_public_no
from app.services.review import open_review

REFUND_OPEN = ("requested", "processing", "manual_required")


class RefundError(ValueError):
    pass


def request_refund(
    db: Session, order_id: int, amount: Decimal, reason: str, user_id: int, attempt_id: int | None = None
) -> Refund:
    order = db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one()
    attempt_id = attempt_id or order.paid_attempt_id
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    if attempt is None or attempt.order_id != order.id or attempt.status != ATT_SUCCEEDED:
        raise RefundError("no_succeeded_payment")
    if db.scalar(select(Refund.id).where(Refund.attempt_id == attempt.id, Refund.status.in_(REFUND_OPEN))):
        raise RefundError("refund_in_progress")
    amount = q(amount, attempt.currency)
    refundable = attempt.amount - attempt.refunded_amount
    if amount <= 0 or amount > refundable:
        raise RefundError("invalid_amount")
    refund = Refund(
        refund_no=new_public_no("RF"),
        order_id=order.id,
        attempt_id=attempt.id,
        amount=amount,
        currency=attempt.currency,
        reason=reason[:1000],
        status="requested",
        requested_by=user_id,
    )
    db.add(refund)
    db.flush()
    if attempt.id == order.paid_attempt_id:
        # Freeze fulfilment for anything not yet delivered.
        order.payment_status = PAY_REFUND_PENDING
        for item in order.items:
            if item.fulfillment_status != FUL_DELIVERED:
                item.fulfillment_status = FUL_CANCELED
    order_event(
        db,
        order.id,
        "refund_requested",
        f"Refund {amount} {attempt.currency} requested",
        actor_user_id=user_id,
        visible=True,
    )
    enqueue(
        db,
        "execute_refund",
        {"refund_id": refund.id, "order_id": order.id},
        dedupe_key=f"refund:{refund.id}",
        max_attempts=4,
    )
    return refund


def execute_refund(db: Session, refund_id: int) -> str:
    refund = db.get(Refund, refund_id)
    if refund is None or refund.status != "requested":
        return "noop"
    attempt = db.get(PaymentAttempt, refund.attempt_id)
    assert attempt is not None
    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == attempt.channel_code)
    ).scalar_one()
    provider = get_provider(channel.provider)
    refund.status = "processing"
    db.commit()  # marks intent before calling the provider; a crash leaves "processing" for reconciliation
    try:
        result = provider.refund(channel, attempt, refund)
    except ProviderError as exc:
        refund = db.get(Refund, refund_id, with_for_update=True)
        assert refund is not None
        if exc.retryable:
            refund.status = "requested"
            refund.error = str(exc)[:1000]
            db.commit()
            raise
        refund.status = "failed"
        refund.error = str(exc)[:1000]
        open_review(
            db,
            kind="refund_failed",
            order_id=refund.order_id,
            dedupe_key=f"refund_failed:{refund.id}",
            summary=f"Refund {refund.refund_no} failed: {exc}",
        )
        db.commit()
        return "failed"
    refund = db.get(Refund, refund_id, with_for_update=True)
    assert refund is not None
    refund.provider_ref = result.provider_ref or refund.provider_ref
    if result.status == "succeeded":
        _finalize(db, refund, succeeded=True)
    elif result.status == "manual_required":
        refund.status = "manual_required"
        open_review(
            db,
            kind="refund_manual",
            order_id=refund.order_id,
            dedupe_key=f"refund_manual:{refund.id}",
            summary=f"Send {refund.amount} {attempt.pay_currency} back manually and record the tx hash",
        )
    elif result.status == "failed":
        refund.status = "failed"
        refund.error = result.error
        open_review(
            db,
            kind="refund_failed",
            order_id=refund.order_id,
            dedupe_key=f"refund_failed:{refund.id}",
            summary=f"Refund {refund.refund_no} failed: {result.error}",
        )
    else:
        refund.status = "processing"  # final state arrives by webhook
    db.commit()
    return refund.status


def apply_refund_result(
    db: Session, refund_no: str | None, *, succeeded: bool, provider_ref: str | None, amount: Decimal | None
) -> str:
    if not refund_no:
        return "unmatched_refund"
    refund = db.execute(
        select(Refund).where(Refund.refund_no == refund_no).with_for_update()
    ).scalar_one_or_none()
    if refund is None:
        return "unmatched_refund"
    if refund.status in ("succeeded", "failed"):
        return "already_applied"
    if provider_ref:
        refund.provider_ref = provider_ref
    if succeeded and amount is not None and amount != refund.amount:
        open_review(
            db,
            kind="refund_amount_mismatch",
            order_id=refund.order_id,
            dedupe_key=f"refund_amount:{refund.id}",
            summary=f"Provider refunded {amount}, requested {refund.amount}",
        )
    _finalize(db, refund, succeeded=succeeded)
    return "refund_succeeded" if succeeded else "refund_failed"


def mark_manual_refund_done(db: Session, refund_id: int, reference: str, user_id: int) -> None:
    refund = db.get(Refund, refund_id, with_for_update=True)
    if refund is None or refund.status != "manual_required":
        raise RefundError("not_manual")
    refund.provider_ref = reference.strip()[:128] or None
    _finalize(db, refund, succeeded=True)
    order_event(
        db,
        refund.order_id,
        "refund_manual_done",
        f"Manual refund recorded ({reference[:66]})",
        actor_user_id=user_id,
    )


def _finalize(db: Session, refund: Refund, *, succeeded: bool) -> None:
    order = db.execute(select(Order).where(Order.id == refund.order_id).with_for_update()).scalar_one()
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.id == refund.attempt_id).with_for_update()
    ).scalar_one()
    refund.completed_at = utcnow()
    is_primary = attempt.id == order.paid_attempt_id
    if not succeeded:
        refund.status = "failed"
        if is_primary and order.payment_status == PAY_REFUND_PENDING:
            # Money was not returned: order stays paid, but cancelled items need a human decision.
            order.payment_status = PAY_PARTIALLY_REFUNDED if order.refunded_total > 0 else PAY_PAID
            open_review(
                db,
                kind="refund_failed",
                order_id=order.id,
                dedupe_key=f"refund_failed:{refund.id}",
                summary=f"Refund {refund.refund_no} failed; decide on re-delivery or retry",
            )
        order_event(db, order.id, "refund_failed", "Refund failed", visible=True)
        return
    refund.status = "succeeded"
    attempt.refunded_amount = attempt.refunded_amount + refund.amount
    if is_primary:
        order.refunded_total = order.refunded_total + refund.amount
        order.payment_status = PAY_REFUNDED if order.refunded_total >= order.total else PAY_PARTIALLY_REFUNDED
        from app.services.agents import adjust_commission_for_refund

        adjust_commission_for_refund(db, order)
    order_event(db, order.id, "refunded", f"Refunded {refund.amount} {refund.currency}", visible=True)
    enqueue(
        db,
        "send_email",
        {"template": "order_refunded", "order_id": order.id, "refund_id": refund.id},
        dedupe_key=f"email:refund:{refund.id}",
    )
