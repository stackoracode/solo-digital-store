"""Simulated payment provider for development and CI. Refuses to work in production.

The mock goes through the same signed-webhook path as real providers, so the success page alone can
never mark an order paid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

from app.config import get_settings
from app.db import utcnow
from app.payments.base import (
    EV_IGNORED,
    EV_PAYMENT_FAILED,
    EV_PAYMENT_SUCCEEDED,
    EV_REFUND_SUCCEEDED,
    CreateResult,
    RefundResult,
    SignatureError,
    WebhookEvent,
)

SIG_HEADER = "x-mock-signature"


def sign(body: bytes) -> str:
    secret = get_settings().mock_webhook_secret.encode()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def build_event(attempt_no: str, outcome: str, amount: str, currency: str, event_id: str) -> bytes:
    return json.dumps(
        {
            "eventType": outcome,
            "eventId": event_id,
            "attemptNo": attempt_no,
            "amount": amount,
            "currency": currency,
        },
        separators=(",", ":"),
    ).encode()


class MockProvider:
    name = "mock"

    def check_available(self, channel, order):  # type: ignore[no-untyped-def]
        return None if get_settings().mock_allowed else "mock_disabled"

    def create_payment(self, db, channel, order, attempt):  # type: ignore[no-untyped-def]
        if not get_settings().mock_allowed:
            raise RuntimeError("mock payments are disabled")
        ttl = get_settings().payment_attempt_ttl_minutes
        return CreateResult(
            pay_amount=attempt.amount,
            pay_currency=attempt.currency,
            expires_at=utcnow() + timedelta(minutes=ttl),
            provider_ref=f"mock_{attempt.attempt_no}",
            checkout_url=f"/pay/mock/{attempt.attempt_no}",
        )

    def parse_webhook(self, channel, headers, body):  # type: ignore[no-untyped-def]
        if not get_settings().mock_allowed:
            raise SignatureError("mock disabled")
        given = headers.get(SIG_HEADER, "")
        if not given or not hmac.compare_digest(given, sign(body)):
            raise SignatureError("bad mock signature")
        data = json.loads(body)
        kind = {
            "payment.succeeded": EV_PAYMENT_SUCCEEDED,
            "payment.failed": EV_PAYMENT_FAILED,
            "refund.succeeded": EV_REFUND_SUCCEEDED,
        }.get(data.get("eventType"), EV_IGNORED)
        return WebhookEvent(
            kind=kind,
            event_type=str(data.get("eventType")),
            event_id=str(data.get("eventId")),
            attempt_no=data.get("attemptNo"),
            refund_no=data.get("refundNo"),
            amount=Decimal(str(data["amount"])) if data.get("amount") is not None else None,
            currency=data.get("currency"),
            provider_payment_id=f"mockpay_{data.get('attemptNo')}",
            raw=data,
        )

    def refund(self, channel, attempt, refund):  # type: ignore[no-untyped-def]
        return RefundResult(status="succeeded", provider_ref=f"mockrf_{refund.refund_no}")
