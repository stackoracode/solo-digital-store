"""Job handlers. Each one must be idempotent: jobs can run more than once after crashes/timeouts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.payments import ATT_PENDING, PaymentAttempt, PaymentChannel

log = logging.getLogger(__name__)


@dataclass
class Reschedule(Exception):
    """Raise to re-run later without consuming the retry budget."""

    delay: float
    note: str = ""


def h_fulfill_order(db: Session, p: dict[str, Any]) -> None:
    from app.services.fulfillment import fulfill_order

    fulfill_order(db, int(p["order_id"]))


def h_procure(db: Session, p: dict[str, Any]) -> None:
    from app.jobs.queue import enqueue
    from app.services.procurement import procure

    outcome = procure(db, int(p["procurement_id"]))
    _follow_up(db, outcome, p, enqueue)


def h_procurement_check(db: Session, p: dict[str, Any]) -> None:
    from app.services.procurement import check

    outcome = check(db, int(p["procurement_id"]))
    if outcome.action == "check_later":
        raise Reschedule(outcome.delay, "supplier pending")
    if outcome.action == "retry":
        raise RuntimeError("supplier unavailable")


def _follow_up(db: Session, outcome: Any, p: dict[str, Any], enqueue: Callable[..., None]) -> None:
    if outcome.action == "check_later":
        enqueue(
            db,
            "procurement_check",
            {"procurement_id": p["procurement_id"], "order_id": p.get("order_id")},
            dedupe_key=f"procurement_check:{p['procurement_id']}",
            delay_seconds=outcome.delay,
            max_attempts=10,
        )
        db.commit()
    elif outcome.action == "retry":
        raise RuntimeError("supplier unavailable; retrying")


def h_execute_refund(db: Session, p: dict[str, Any]) -> None:
    from app.services.refunds import execute_refund

    execute_refund(db, int(p["refund_id"]))


def h_send_email(db: Session, p: dict[str, Any]) -> None:
    from app.services.notify import send_email

    send_email(db, str(p["template"]), p)


def h_usdt_scan(db: Session, p: dict[str, Any]) -> None:
    from app.payments.usdt.rpc import RpcError
    from app.payments.usdt.scanner import mark_channel_error, scan_network

    try:
        scan_network(db, str(p["network"]))
    except RpcError as exc:
        mark_channel_error(db, str(p["network"]), str(exc))
        raise


def h_expire(db: Session, p: dict[str, Any]) -> None:
    from datetime import timedelta

    from app.jobs.queue import enqueue
    from app.models.ops import PROC_SUBMITTED, PROC_UNKNOWN, Procurement
    from app.services.orders import expire_stale_orders
    from app.services.payments import expire_attempts

    expire_attempts(db)
    expire_stale_orders(db)
    # Safety net: an ambiguous procurement whose follow-up job was lost (crash) gets re-checked.
    stale = (
        db.execute(
            select(Procurement.id)
            .where(
                Procurement.status.in_((PROC_SUBMITTED, PROC_UNKNOWN)),
                Procurement.updated_at < utcnow() - timedelta(minutes=10),
            )
            .limit(100)
        )
        .scalars()
        .all()
    )
    bucket = int(utcnow().timestamp() // 600)
    for proc_id in stale:
        enqueue(
            db,
            "procurement_check",
            {"procurement_id": proc_id},
            dedupe_key=f"procurement_check:{proc_id}:sweep:{bucket}",
            max_attempts=5,
        )
    db.commit()


def h_reap(db: Session, p: dict[str, Any]) -> None:
    from datetime import timedelta

    from sqlalchemy import delete

    from app.jobs.queue import reap_stuck
    from app.models.ops import JOB_SUCCEEDED, Job

    reap_stuck(db)
    db.execute(
        delete(Job).where(Job.status == JOB_SUCCEEDED, Job.finished_at < utcnow() - timedelta(days=14))
    )
    # Periodic maintenance jobs are high-volume and low-value once done.
    db.execute(
        delete(Job).where(
            Job.status == JOB_SUCCEEDED,
            Job.kind.in_(("expire", "reap", "usdt_scan")),
            Job.finished_at < utcnow() - timedelta(hours=6),
        )
    )
    db.commit()


def h_waffo_reconcile(db: Session, p: dict[str, Any]) -> None:
    """Lost-webhook safety net. Read-only: surfaces discrepancies to a human, never credits."""
    from app.payments.waffo import WaffoProvider
    from app.services.review import open_review

    attempt = db.get(PaymentAttempt, int(p["attempt_id"]))
    if attempt is None or attempt.status not in (ATT_PENDING, "expired"):
        return
    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == attempt.channel_code)
    ).scalar_one()
    if not channel.enabled:
        return
    payments = WaffoProvider().lookup_payment_status(attempt.attempt_no)
    if any(str(x.get("status")) == "succeeded" for x in payments):
        open_review(
            db,
            kind="waffo_missing_webhook",
            order_id=attempt.order_id,
            attempt_id=attempt.id,
            dedupe_key=f"waffo_missing_webhook:{attempt.id}",
            summary=f"Waffo reports a succeeded payment for {attempt.attempt_no} but no verified webhook "
            "was processed; check webhook delivery logs, then resend or credit manually",
        )
        db.commit()


HANDLERS: dict[str, Callable[[Session, dict[str, Any]], None]] = {
    "fulfill_order": h_fulfill_order,
    "procure": h_procure,
    "procurement_check": h_procurement_check,
    "execute_refund": h_execute_refund,
    "send_email": h_send_email,
    "usdt_scan": h_usdt_scan,
    "expire": h_expire,
    "reap": h_reap,
    "waffo_reconcile": h_waffo_reconcile,
}
