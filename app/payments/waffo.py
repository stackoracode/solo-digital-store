"""Waffo Pancake adapter (merchant of record). REST + manual RSA-SHA256 signing per docs.waffo.ai.

There is no official Python SDK (official SDKs: TypeScript `@waffo/pancake-ts`, Go). Signing follows
https://docs.waffo.ai/api-reference/authentication and webhook verification follows
https://docs.waffo.ai/api-reference/webhooks (both re-checked 2026-10-23, see docs/payment-feasibility.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import get_settings
from app.core.money import display
from app.db import utcnow
from app.payments.base import (
    EV_IGNORED,
    EV_PAYMENT_SUCCEEDED,
    EV_REFUND_FAILED,
    EV_REFUND_SUCCEEDED,
    CreateResult,
    ProviderError,
    RefundResult,
    SignatureError,
    WebhookEvent,
)

CREATE_SESSION_PATH = "/v1/actions/checkout/create-session"
CREATE_REFUND_PATH = "/v1/actions/refund-ticket/create-ticket"
GRAPHQL_PATH = "/v1/graphql"
SIGNATURE_HEADER = "x-waffo-signature"
# Retries replay the original signature header; docs recommend a 45 minute window, not 5.
WEBHOOK_TOLERANCE_MS = 45 * 60 * 1000
LANGUAGE_MAP = {"en-US": "en", "zh-CN": "zh-Hans"}


def _normalize_pem(value: str, kind: str) -> str:
    value = value.strip().replace("\\n", "\n")
    if "-----BEGIN" in value:
        return value
    body = "\n".join(value[i : i + 64] for i in range(0, len(value), 64))
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----\n"


def canonical_request(method: str, path: str, timestamp: str, body: str) -> str:
    body_hash = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    return f"{method}\n{path}\n{timestamp}\n{body_hash}"


def sign_request(private_key_pem: str, method: str, path: str, timestamp: str, body: str) -> str:
    key = serialization.load_pem_private_key(
        _normalize_pem(private_key_pem, "PRIVATE KEY").encode(), password=None
    )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ProviderError("Waffo private key must be RSA", retryable=False)
    sig = key.sign(
        canonical_request(method, path, timestamp, body).encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.b64encode(sig).decode()


def verify_webhook_signature(
    raw_body: str, header: str, public_key_pem: str, now_ms: int | None = None
) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1 or not t.isdigit():
        return False
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now_ms - int(t)) > WEBHOOK_TOLERANCE_MS:
        return False
    try:
        key = serialization.load_pem_public_key(_normalize_pem(public_key_pem, "PUBLIC KEY").encode())
        if not isinstance(key, rsa.RSAPublicKey):
            return False
        key.verify(base64.b64decode(v1), f"{t}.{raw_body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError):
        return False


class WaffoClient:
    def __init__(self, merchant_id: str, private_key: str, base_url: str, timeout: float = 15.0) -> None:
        self.merchant_id = merchant_id
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.merchant_id or not self.private_key:
            raise ProviderError(
                "Waffo credentials are not configured", retryable=False, code="not_configured"
            )
        body = json.dumps(payload, separators=(",", ":"))
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Merchant-Id": self.merchant_id,
            "X-Timestamp": ts,
            "X-Signature": sign_request(self.private_key, "POST", path, ts, body),
        }
        try:
            resp = httpx.post(self.base_url + path, content=body, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("Waffo request timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Waffo network error: {exc.__class__.__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            message = ""
            errors = data.get("errors") if isinstance(data, dict) else None
            if isinstance(errors, list) and errors:
                message = str(errors[0].get("message", ""))[:300]
            raise ProviderError(
                f"Waffo HTTP {resp.status_code}: {message}",
                retryable=resp.status_code >= 500 or resp.status_code == 409 and "retry" in message.lower(),
                code=f"http_{resp.status_code}",
            )
        return data.get("data", {}) if isinstance(data, dict) else {}


def client_from_settings() -> WaffoClient:
    s = get_settings()
    return WaffoClient(s.waffo_merchant_id, s.waffo_private_key, s.waffo_api_base)


class WaffoProvider:
    name = "waffo"

    def __init__(self, client: WaffoClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> WaffoClient:
        return self._client or client_from_settings()

    def check_available(self, channel, order):  # type: ignore[no-untyped-def]
        settings = get_settings()
        if not settings.waffo_merchant_id or not settings.waffo_private_key:
            return "not_configured"
        from sqlalchemy.orm import object_session

        from app.models.catalog import Product

        db = object_session(order)
        for item in order.items:
            product = db.get(Product, item.product_id) if db else None
            if product is None or not product.waffo_product_id:
                return "product_not_mapped"
            if channel.mode == "live" and not product.waffo_approved:
                # Live payments only for categories approved in Waffo merchant review.
                return "product_not_approved"
        if channel.mode == "live" and not settings.waffo_live_enabled:
            return "live_disabled"
        return None

    def create_payment(self, db, channel, order, attempt):  # type: ignore[no-untyped-def]
        from app.models.catalog import Product

        item = order.items[0]
        product = db.get(Product, item.product_id)
        ttl_minutes = get_settings().payment_attempt_ttl_minutes
        base = get_settings().public_base_url.rstrip("/")
        payload: dict[str, Any] = {
            "productId": product.waffo_product_id,
            "currency": attempt.currency,
            # API-key-only override; taxIncluded keeps the buyer-visible total equal to our price.
            "priceSnapshot": {
                "amount": display(attempt.amount, attempt.currency),
                "taxIncluded": True,
                "taxCategory": channel.config.get("tax_category", "digital_goods"),
            },
            "buyerEmail": order.email,
            "expiresInSeconds": ttl_minutes * 60,
            "orderMerchantExternalId": attempt.attempt_no,
            "metadata": {"order_no": order.order_no},
        }
        if base.startswith("https://"):
            payload["successUrl"] = f"{base}/o/{order.order_no}/return"
        lang = LANGUAGE_MAP.get(order.locale)
        if lang:
            payload["language"] = lang
        data = self.client.post(CREATE_SESSION_PATH, payload)
        url = data.get("checkoutUrl")
        if not url or not str(url).startswith("https://"):
            raise ProviderError("Waffo returned no checkout URL", retryable=False, code="bad_response")
        expires_at = utcnow() + timedelta(minutes=ttl_minutes)
        if data.get("expiresAt"):
            try:
                expires_at = datetime.fromisoformat(str(data["expiresAt"]).replace("Z", "+00:00")).astimezone(
                    UTC
                )
            except ValueError:
                pass
        return CreateResult(
            pay_amount=attempt.amount,
            pay_currency=attempt.currency,
            expires_at=expires_at,
            provider_ref=data.get("sessionId"),
            checkout_url=str(url),
        )

    def parse_webhook(self, channel, headers, body):  # type: ignore[no-untyped-def]
        settings = get_settings()
        key = (
            settings.waffo_webhook_public_key_prod
            if channel.mode == "live"
            else settings.waffo_webhook_public_key_test
        )
        if not key:
            raise SignatureError("webhook public key not configured")
        raw = body.decode("utf-8")
        if not verify_webhook_signature(raw, headers.get(SIGNATURE_HEADER, ""), key):
            raise SignatureError("invalid Waffo signature")
        event = json.loads(raw)
        expected_mode = "prod" if channel.mode == "live" else "test"
        if event.get("mode") != expected_mode:
            raise SignatureError(f"environment mismatch: event mode {event.get('mode')}")
        data = event.get("data") or {}
        event_type = str(event.get("eventType", ""))
        kind = EV_IGNORED
        amount = None
        if event_type == "order.completed":
            kind = EV_PAYMENT_SUCCEEDED
            raw_amount = data.get("chargedAmount", data.get("amount"))
            amount = Decimal(str(raw_amount)) if raw_amount is not None else None
        elif event_type in ("refund.succeeded", "refund.failed"):
            kind = EV_REFUND_SUCCEEDED if data.get("refundStatus") == "succeeded" else EV_REFUND_FAILED
            raw_amount = data.get("refundedAmount", data.get("amount"))
            amount = Decimal(str(raw_amount)) if raw_amount is not None else None
        return WebhookEvent(
            kind=kind,
            event_type=event_type,
            event_id=str(event.get("eventId") or event.get("id") or ""),
            attempt_no=data.get("orderMerchantExternalId"),
            refund_no=data.get("refundTicketMerchantExternalId"),
            amount=amount,
            currency=data.get("currency"),
            provider_payment_id=data.get("paymentId"),
            provider_refund_id=event.get("id") if kind != EV_PAYMENT_SUCCEEDED else None,
            mode=event.get("mode"),
            failure_reason=str(data.get("paymentFailureReason", "")),
            raw=event,
        )

    def refund(self, channel, attempt, refund):  # type: ignore[no-untyped-def]
        if not attempt.provider_payment_id:
            return RefundResult(status="failed", error="missing Waffo paymentId on attempt")
        data = self.client.post(
            CREATE_REFUND_PATH,
            {
                "paymentId": attempt.provider_payment_id,
                "reason": refund.reason[:500] or "merchant refund",
                "requestedAmount": {
                    "amount": display(refund.amount, refund.currency),
                    "currency": refund.currency,
                },
                "refundTicketMerchantExternalId": refund.refund_no,
            },
        )
        ticket = data.get("ticket") or {}
        status = ticket.get("status", "processing")
        # Merchant-initiated tickets go straight to processing; final state arrives via refund.* webhook.
        return RefundResult(
            status="failed" if status in ("rejected", "failed") else "processing",
            provider_ref=ticket.get("id"),
        )

    def lookup_payment_status(self, attempt_no: str) -> list[dict[str, Any]]:
        """Read-only reconciliation via GraphQL (documented fields only). Never auto-credits."""
        query = (
            "query($ref: String!) { payments(filter: { orderMerchantExternalId: { eq: $ref } }) "
            "{ id orderId status refundStatus orderMerchantExternalId createdAt } }"
        )
        data = self.client.post(GRAPHQL_PATH, {"query": query, "variables": {"ref": attempt_no}})
        payments = data.get("payments") if isinstance(data, dict) else None
        return payments if isinstance(payments, list) else []
