"""Payment Provider Adapter contract. Order logic never contains channel-specific code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.orders import Order, Refund
    from app.models.payments import PaymentAttempt, PaymentChannel

# Normalised webhook outcomes
EV_PAYMENT_SUCCEEDED = "payment_succeeded"
EV_PAYMENT_FAILED = "payment_failed"
EV_REFUND_SUCCEEDED = "refund_succeeded"
EV_REFUND_FAILED = "refund_failed"
EV_IGNORED = "ignored"


class ProviderError(Exception):
    """Provider call failed. retryable=False means the request itself is wrong (4xx)."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "provider_error") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class SignatureError(Exception):
    pass


@dataclass
class CreateResult:
    pay_amount: Decimal
    pay_currency: str
    expires_at: datetime
    provider_ref: str | None = None
    checkout_url: str | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    kind: str
    event_type: str
    event_id: str | None
    attempt_no: str | None = None
    refund_no: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    provider_payment_id: str | None = None
    provider_refund_id: str | None = None
    mode: str | None = None
    failure_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RefundResult:
    status: str  # processing | succeeded | failed | manual_required
    provider_ref: str | None = None
    error: str = ""


class PaymentProvider(Protocol):
    name: str

    def check_available(self, channel: PaymentChannel, order: Order) -> str | None:
        """Return None if this channel may be offered for the order, else a reason code."""

    def create_payment(
        self, db: Session, channel: PaymentChannel, order: Order, attempt: PaymentAttempt
    ) -> CreateResult: ...

    def parse_webhook(self, channel: PaymentChannel, headers: dict[str, str], body: bytes) -> WebhookEvent:
        """Verify authenticity (raise SignatureError) and normalise the event."""

    def refund(self, channel: PaymentChannel, attempt: PaymentAttempt, refund: Refund) -> RefundResult: ...
