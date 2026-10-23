"""Payment orchestration. The ONLY path to `paid` is apply_payment_success() with verified evidence."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.money import fee
from app.db import utcnow
from app.jobs.queue import enqueue
from app.models.orders import (
    FUL_QUEUED,
    LC_ACTIVE,
    LC_CANCELED,
    LC_EXPIRED,
    LC_PENDING_PAYMENT,
    PAY_UNPAID,
    Order,
)
from app.models.payments import (
    ACTIVE_ATTEMPT_STATES,
    ATT_CREATED,
    ATT_EXPIRED,
    ATT_FAILED,
    ATT_NEEDS_REVIEW,
    ATT_PENDING,
    ATT_SUCCEEDED,
    PaymentAttempt,
    PaymentChannel,
    PaymentEvent,
)
from app.payments.base import (
    EV_PAYMENT_FAILED,
    EV_PAYMENT_SUCCEEDED,
    EV_REFUND_FAILED,
    EV_REFUND_SUCCEEDED,
    ProviderError,
    SignatureError,
)
from app.payments.registry import get_provider
from app.services.audit import order_event
from app.services.orders import new_public_no
from app.services.review import open_review

log = logging.getLogger(__name__)


class PaymentError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def lock_order(db: Session, order_id: int) -> Order:
    return db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one()


def available_channels(db: Session, order: Order) -> list[tuple[PaymentChannel, str | None]]:
    """All enabled channels with an availability reason (None = usable)."""
    channels = (
        db.execute(
            select(PaymentChannel)
            .where(PaymentChannel.enabled.is_(True))
            .order_by(PaymentChannel.sort, PaymentChannel.id)
        )
        .scalars()
        .all()
    )
    from app.models.catalog import Product

    allowed: set[str] | None = None
    for item in order.items:
        product = db.get(Product, item.product_id)
        if product is not None and product.allowed_channels:
            allowed = (
                set(product.allowed_channels) if allowed is None else allowed & set(product.allowed_channels)
            )
    out: list[tuple[PaymentChannel, str | None]] = []
    for ch in channels:
        reason: str | None
        if allowed is not None and ch.code not in allowed:
            continue
        if ch.min_amount is not None and order.total < ch.min_amount:
            reason = "below_min"
        elif ch.max_amount is not None and order.total > ch.max_amount:
            reason = "above_max"
        else:
            reason = get_provider(ch.provider).check_available(ch, order)
        out.append((ch, reason))
    return out


def start_payment(db: Session, order: Order, channel_code: str) -> PaymentAttempt:
    """Create (or reuse) the active attempt for (order, channel). Safe against double clicks."""
    order = lock_order(db, order.id)
    if order.payment_status != PAY_UNPAID or order.lifecycle != LC_PENDING_PAYMENT:
        raise PaymentError("order_not_payable")
    if order.expires_at < utcnow():
        raise PaymentError("order_expired")
    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == channel_code)
    ).scalar_one_or_none()
    if channel is None or not channel.enabled:
        raise PaymentError("channel_unavailable")
    usable = {c.code: r for c, r in available_channels(db, order)}
    if channel_code not in usable or usable[channel_code] is not None:
        raise PaymentError("channel_unavailable")

    existing = db.execute(
        select(PaymentAttempt).where(
            PaymentAttempt.order_id == order.id,
            PaymentAttempt.channel_code == channel_code,
            PaymentAttempt.status.in_(ACTIVE_ATTEMPT_STATES),
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.expires_at > utcnow() and existing.status == ATT_PENDING:
            db.commit()
            return existing
        existing.status = ATT_EXPIRED if existing.expires_at <= utcnow() else ATT_FAILED
        db.flush()

    provider = get_provider(channel.provider)
    attempt = PaymentAttempt(
        attempt_no=new_public_no("PA"),
        order_id=order.id,
        channel_code=channel.code,
        provider=channel.provider,
        is_live=channel.mode == "live",
        status=ATT_CREATED,
        amount=order.total,
        currency=order.currency,
        pay_amount=order.total,
        pay_currency=order.currency,
        expires_at=utcnow(),
        extra={},
    )
    db.add(attempt)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PaymentError("attempt_in_progress") from None
    try:
        result = provider.create_payment(db, channel, order, attempt)
    except ProviderError as exc:
        attempt.status = ATT_FAILED
        attempt.error_code = exc.code
        attempt.error_message = str(exc)[:1000]
        channel.last_error = f"{utcnow():%Y-%m-%d %H:%M} {exc}"[:1000]
        db.commit()
        log.warning("payment creation failed", extra={"path": channel.code})
        raise PaymentError("provider_error") from exc
    attempt.status = ATT_PENDING
    attempt.pay_amount = result.pay_amount
    attempt.pay_currency = result.pay_currency
    attempt.expires_at = result.expires_at
    attempt.provider_ref = result.provider_ref
    attempt.checkout_url = result.checkout_url
    attempt.fx_rate = result.fx_rate
    attempt.fx_source = result.fx_source
    attempt.fx_at = utcnow() if result.fx_rate is not None else None
    attempt.extra = result.extra
    order_event(db, order.id, "payment_started", f"Payment started via {channel.display_name}", visible=True)
    if channel.provider == "waffo":
        # Lost-webhook safety net: read-only status check later; never auto-credits.
        enqueue(
            db,
            "waffo_reconcile",
            {"attempt_id": attempt.id},
            delay_seconds=attempt_ttl_seconds(attempt) + 300,
            dedupe_key=f"waffo_reconcile:{attempt.id}",
        )
    db.commit()
    return attempt


def attempt_ttl_seconds(attempt: PaymentAttempt) -> float:
    return max(60.0, (attempt.expires_at - utcnow()).total_seconds())


def apply_payment_success(
    db: Session,
    attempt_id: int,
    *,
    amount: Decimal,
    currency: str,
    provider_payment_id: str | None,
    source: str,
    allow_late: bool = False,
    actor_user_id: int | None = None,
) -> str:
    """Idempotently record verified money received for an attempt. Caller commits.

    Returns one of: already_applied | paid | duplicate_payment | underpaid | currency_mismatch | late_review
    """
    attempt = db.execute(select(PaymentAttempt).where(PaymentAttempt.id == attempt_id)).scalar_one()
    order = lock_order(db, attempt.order_id)  # order row lock serialises all payment/refund/fulfilment work
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
    ).scalar_one()
    if attempt.status == ATT_SUCCEEDED:
        return "already_applied"
    if provider_payment_id:
        attempt.provider_payment_id = provider_payment_id
    if currency.upper() != attempt.pay_currency.upper():
        attempt.status = ATT_NEEDS_REVIEW
        open_review(
            db,
            kind="currency_mismatch",
            order_id=order.id,
            attempt_id=attempt.id,
            dedupe_key=f"currency_mismatch:{attempt.id}",
            summary=f"Paid {amount} {currency}, expected {attempt.pay_amount} {attempt.pay_currency}",
        )
        return "currency_mismatch"
    if amount < attempt.pay_amount:
        attempt.status = ATT_NEEDS_REVIEW
        open_review(
            db,
            kind="underpaid",
            order_id=order.id,
            attempt_id=attempt.id,
            dedupe_key=f"underpaid:{attempt.id}",
            summary=f"Underpaid: received {amount}, expected {attempt.pay_amount} {currency}",
        )
        order_event(
            db, order.id, "underpaid", "Payment amount is lower than required; under review", visible=True
        )
        return "underpaid"
    # A provider webhook may legitimately arrive after our attempt timer (payment finished just before
    # session expiry); only an expired/canceled *order* or a canceled attempt is treated as late here.
    # On-chain lateness is decided by the scanner, which knows when the transfer was first seen.
    if not allow_late and (attempt.status == "canceled" or order.lifecycle in (LC_EXPIRED, LC_CANCELED)):
        attempt.status = ATT_NEEDS_REVIEW
        open_review(
            db,
            kind="late_payment",
            order_id=order.id,
            attempt_id=attempt.id,
            dedupe_key=f"late_payment:{attempt.id}",
            summary=f"Payment of {amount} {currency} arrived after the attempt/order expired",
        )
        order_event(db, order.id, "late_payment", "Late payment received; under manual review", visible=True)
        return "late_review"

    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == attempt.channel_code)
    ).scalar_one()
    attempt.status = ATT_SUCCEEDED
    attempt.paid_at = utcnow()
    attempt.received_amount = max(attempt.received_amount, amount)
    attempt.fee_amount = fee(attempt.amount, channel.fee_percent, channel.fee_fixed, attempt.currency)
    channel.last_event_at = utcnow()

    if amount > attempt.pay_amount:
        open_review(
            db,
            kind="overpaid",
            severity="medium",
            order_id=order.id,
            attempt_id=attempt.id,
            dedupe_key=f"overpaid:{attempt.id}",
            summary=f"Overpaid by {amount - attempt.pay_amount} {currency}; refund the difference",
        )

    if order.payment_status != PAY_UNPAID or order.paid_attempt_id is not None:
        attempt.is_duplicate = True
        open_review(
            db,
            kind="duplicate_payment",
            order_id=order.id,
            attempt_id=attempt.id,
            dedupe_key=f"duplicate_payment:{attempt.id}",
            summary=f"Order {order.order_no} was already paid; attempt {attempt.attempt_no} must be refunded",
        )
        order_event(db, order.id, "duplicate_payment", f"Duplicate payment via {attempt.channel_code}")
        return "duplicate_payment"

    order.payment_status = "paid"
    order.paid_attempt_id = attempt.id
    order.paid_at = utcnow()
    order.lifecycle = LC_ACTIVE
    order.fulfillment_status = FUL_QUEUED
    order.fee_total = attempt.fee_amount
    order_event(
        db,
        order.id,
        "paid",
        f"Payment confirmed ({source})",
        visible=True,
        data={"attempt": attempt.attempt_no, "source": source},
        actor_user_id=actor_user_id,
    )
    # Outbox: committed atomically with the paid state. Dedupe key => one fulfilment per order.
    enqueue(
        db,
        "fulfill_order",
        {"order_id": order.id},
        dedupe_key=f"fulfill_order:{order.id}",
        timeout_seconds=120,
    )
    enqueue(
        db,
        "send_email",
        {"template": "order_paid", "order_id": order.id},
        dedupe_key=f"email:paid:{order.id}",
    )
    from app.services.agents import create_commission

    create_commission(db, order)
    return "paid"


def apply_payment_failure(db: Session, attempt: PaymentAttempt, reason: str) -> None:
    if attempt.status in ACTIVE_ATTEMPT_STATES:
        attempt.status = ATT_FAILED
        attempt.error_message = reason[:1000]
        order_event(db, attempt.order_id, "payment_failed", "Payment failed", visible=True)


def handle_webhook(
    db: Session, channel_code: str, headers: dict[str, str], body: bytes, remote_ip: str
) -> tuple[int, str]:
    """Verify, persist and apply a provider notification. Returns (http_status, text)."""
    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == channel_code)
    ).scalar_one_or_none()
    if channel is None:
        return 404, "unknown channel"
    provider = get_provider(channel.provider)
    try:
        event = provider.parse_webhook(channel, headers, body)
    except (SignatureError, ValueError, json.JSONDecodeError) as exc:
        db.add(
            PaymentEvent(
                provider=channel.provider,
                event_type="invalid",
                event_id=None,
                signature_valid=False,
                payload={"error": str(exc)[:300], "size": len(body)},
                received_at=utcnow(),
                result="rejected",
                error=str(exc)[:500],
                remote_ip=remote_ip,
            )
        )
        db.commit()
        return 401, "invalid signature"

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    inserted = db.execute(
        pg_insert(PaymentEvent)
        .values(
            provider=channel.provider,
            event_type=event.event_type,
            event_id=event.event_id,
            signature_valid=True,
            payload=event.raw,
            received_at=utcnow(),
            result="received",
            error="",
            remote_ip=remote_ip,
        )
        .on_conflict_do_nothing(index_elements=["provider", "event_type", "event_id"])
        .returning(PaymentEvent.id)
    ).scalar_one_or_none()
    if inserted is None:
        db.commit()
        return 200, "duplicate"  # already processed; acknowledge so the provider stops retrying
    record = db.get(PaymentEvent, inserted)
    assert record is not None
    channel.last_event_at = utcnow()

    result = "ignored"
    if event.kind in (EV_PAYMENT_SUCCEEDED, EV_PAYMENT_FAILED):
        attempt = None
        if event.attempt_no:
            attempt = db.execute(
                select(PaymentAttempt).where(
                    PaymentAttempt.attempt_no == event.attempt_no, PaymentAttempt.provider == channel.provider
                )
            ).scalar_one_or_none()
        if attempt is None:
            result = "unmatched"
            open_review(
                db,
                kind="unmatched_payment",
                dedupe_key=f"unmatched:{channel.provider}:{event.event_id}",
                summary=f"{channel.provider} {event.event_type} for unknown reference {event.attempt_no}",
                data={"event_id": event.event_id},
            )
        else:
            record.attempt_id = attempt.id
            if attempt.is_live != (channel.mode == "live"):
                result = "mode_mismatch"
                open_review(
                    db,
                    kind="mode_mismatch",
                    attempt_id=attempt.id,
                    order_id=attempt.order_id,
                    dedupe_key=f"mode_mismatch:{attempt.id}",
                    summary="Event environment differs from attempt",
                )
            elif event.kind == EV_PAYMENT_SUCCEEDED:
                if event.amount is None or not event.currency:
                    result = "missing_amount"
                    open_review(
                        db,
                        kind="missing_amount",
                        attempt_id=attempt.id,
                        order_id=attempt.order_id,
                        dedupe_key=f"missing_amount:{attempt.id}",
                        summary="Provider reported success without amount; verify in provider dashboard",
                    )
                else:
                    result = apply_payment_success(
                        db,
                        attempt.id,
                        amount=event.amount,
                        currency=event.currency,
                        provider_payment_id=event.provider_payment_id,
                        source=f"webhook:{channel.provider}",
                    )
            else:
                apply_payment_failure(db, attempt, event.failure_reason or "provider reported failure")
                result = "failed"
    elif event.kind in (EV_REFUND_SUCCEEDED, EV_REFUND_FAILED):
        from app.services.refunds import apply_refund_result

        result = apply_refund_result(
            db,
            event.refund_no,
            succeeded=event.kind == EV_REFUND_SUCCEEDED,
            provider_ref=event.provider_refund_id,
            amount=event.amount,
        )
    record.result = result
    record.processed_at = utcnow()
    db.commit()
    return 200, result


def expire_attempts(db: Session, limit: int = 500) -> int:
    rows = (
        db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.status.in_(ACTIVE_ATTEMPT_STATES), PaymentAttempt.expires_at < utcnow())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for attempt in rows:
        if attempt.received_amount > 0:
            attempt.status = ATT_NEEDS_REVIEW
            open_review(
                db,
                kind="underpaid",
                order_id=attempt.order_id,
                attempt_id=attempt.id,
                dedupe_key=f"underpaid:{attempt.id}",
                summary=f"Partial payment {attempt.received_amount}/{attempt.pay_amount} "
                f"{attempt.pay_currency} at expiry",
            )
        else:
            attempt.status = ATT_EXPIRED
    db.commit()
    return len(rows)
