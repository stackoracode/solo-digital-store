"""Waffo adapter: request signing, webhook verification, environment isolation, live gating."""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select

from app.models.orders import Order
from app.models.payments import PaymentChannel
from app.payments import waffo
from app.payments.base import SignatureError
from app.services.payments import handle_webhook
from tests.conftest import drain, enable_channel, make_order, make_product, start


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return key, priv, pub


def _sign_webhook(key, body: str, t_ms: int | None = None) -> str:
    t = str(t_ms if t_ms is not None else int(time.time() * 1000))
    sig = key.sign(f"{t}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"t={t},v1={base64.b64encode(sig).decode()}"


def test_request_signature_matches_documented_canonical_form():
    key, priv, _ = _keypair()
    body = '{"name":"My Store"}'
    sig = waffo.sign_request(priv, "POST", "/v1/actions/store/create-store", "1705312200", body)
    body_hash = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    canonical = f"POST\n/v1/actions/store/create-store\n1705312200\n{body_hash}"
    key.public_key().verify(base64.b64decode(sig), canonical.encode(), padding.PKCS1v15(), hashes.SHA256())


def test_webhook_signature_window_and_tampering():
    key, _, pub = _keypair()
    body = '{"eventType":"order.completed"}'
    assert waffo.verify_webhook_signature(body, _sign_webhook(key, body), pub)
    assert not waffo.verify_webhook_signature(body + " ", _sign_webhook(key, body), pub)
    old = int(time.time() * 1000) - 46 * 60 * 1000
    assert not waffo.verify_webhook_signature(body, _sign_webhook(key, body, old), pub)
    # Retries replay the original header ~31 minutes later: must still verify.
    retry = int(time.time() * 1000) - 31 * 60 * 1000
    assert waffo.verify_webhook_signature(body, _sign_webhook(key, body, retry), pub)
    assert not waffo.verify_webhook_signature(body, "garbage", pub)


@pytest.fixture
def waffo_env(db, store, monkeypatch):
    from app.config import get_settings

    key, priv, pub = _keypair()
    s = get_settings()
    monkeypatch.setattr(s, "waffo_merchant_id", "MER_test")
    monkeypatch.setattr(s, "waffo_private_key", priv)
    monkeypatch.setattr(s, "waffo_webhook_public_key_test", pub)
    sent: list[dict] = []

    def fake_post(self, path, payload):
        sent.append({"path": path, "payload": payload})
        if path == waffo.CREATE_SESSION_PATH:
            return {
                "sessionId": "cs_123",
                "checkoutUrl": "https://checkout.waffo.ai/s/cs_123",
                "expiresAt": "2099-01-01T00:00:00.000Z",
            }
        if path == waffo.CREATE_REFUND_PATH:
            return {"ticket": {"id": "TKT_1", "status": "processing"}}
        return {}

    monkeypatch.setattr(waffo.WaffoClient, "post", fake_post)
    enable_channel(db, "waffo")
    return key, sent


def _waffo_order(db, store):
    product = make_product(db, store, codes=2, waffo_product_id="PROD_abc")
    order, _ = make_order(db, store, product)
    return order


def _event(attempt_no: str, mode: str = "test", amount: str = "10.00", event_id: str = "PAY_1") -> str:
    return json.dumps(
        {
            "id": event_id,
            "eventId": event_id,
            "eventType": "order.completed",
            "mode": mode,
            "timestamp": "2026-10-23T00:00:00.000Z",
            "storeId": "STO_1",
            "data": {
                "orderId": "ORD_1",
                "orderStatus": "completed",
                "currency": "USD",
                "chargedAmount": amount,
                "amount": amount,
                "orderMerchantExternalId": attempt_no,
                "paymentId": event_id,
                "paymentStatus": "succeeded",
                "buyerEmail": "buyer@example.com",
            },
        },
        separators=(",", ":"),
    )


def test_checkout_session_payload(db, store, waffo_env):
    _, sent = waffo_env
    order = _waffo_order(db, store)
    attempt = start(db, order, "waffo")
    assert attempt.checkout_url.startswith("https://checkout.waffo.ai/")
    payload = sent[0]["payload"]
    assert payload["productId"] == "PROD_abc"
    assert payload["priceSnapshot"] == {
        "amount": "10.00",
        "taxIncluded": True,
        "taxCategory": "digital_goods",
    }
    assert payload["orderMerchantExternalId"] == attempt.attempt_no
    assert payload["currency"] == "USD"


def test_verified_webhook_pays_and_duplicate_is_ignored(db, store, waffo_env):
    key, _ = waffo_env
    order = _waffo_order(db, store)
    attempt = start(db, order, "waffo")
    body = _event(attempt.attempt_no)
    headers = {waffo.SIGNATURE_HEADER: _sign_webhook(key, body)}
    assert handle_webhook(db, "waffo", headers, body.encode(), "1.2.3.4") == (200, "paid")
    assert handle_webhook(db, "waffo", headers, body.encode(), "1.2.3.4") == (200, "duplicate")
    drain()
    db.expire_all()
    assert db.get(Order, order.id).fulfillment_status == "delivered"


def test_production_event_rejected_by_test_channel(db, store, waffo_env):
    key, _ = waffo_env
    order = _waffo_order(db, store)
    attempt = start(db, order, "waffo")
    body = _event(attempt.attempt_no, mode="prod")
    status, _ = handle_webhook(
        db, "waffo", {waffo.SIGNATURE_HEADER: _sign_webhook(key, body)}, body.encode(), "1.2.3.4"
    )
    assert status == 401
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_forged_webhook_rejected(db, store, waffo_env):
    order = _waffo_order(db, store)
    attempt = start(db, order, "waffo")
    other_key, _, _ = _keypair()
    body = _event(attempt.attempt_no)
    status, _ = handle_webhook(
        db, "waffo", {waffo.SIGNATURE_HEADER: _sign_webhook(other_key, body)}, body.encode(), "x"
    )
    assert status == 401


def test_live_mode_requires_product_approval_and_flag(db, store, waffo_env):
    from app.services.payments import available_channels

    order = _waffo_order(db, store)
    ch = db.execute(select(PaymentChannel).where(PaymentChannel.code == "waffo")).scalar_one()
    ch.mode = "live"
    db.commit()
    reasons = {c.code: r for c, r in available_channels(db, order)}
    assert reasons["waffo"] == "product_not_approved"


def test_unmapped_product_cannot_use_waffo(db, store, waffo_env):
    from app.services.payments import available_channels

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    reasons = {c.code: r for c, r in available_channels(db, order)}
    assert reasons["waffo"] == "product_not_mapped"


def test_refund_ticket_then_webhook(db, store, waffo_env):
    from app.services.refunds import request_refund

    key, sent = waffo_env
    order = _waffo_order(db, store)
    attempt = start(db, order, "waffo")
    body = _event(attempt.attempt_no)
    handle_webhook(db, "waffo", {waffo.SIGNATURE_HEADER: _sign_webhook(key, body)}, body.encode(), "x")
    drain()
    from decimal import Decimal

    refund = request_refund(db, order.id, Decimal("10.00"), "defective", user_id=None)
    db.commit()
    drain()
    assert sent[-1]["path"] == waffo.CREATE_REFUND_PATH
    assert sent[-1]["payload"]["paymentId"] == "PAY_1"
    refund_body = json.dumps(
        {
            "id": "REF_1",
            "eventId": "REF_1",
            "eventType": "refund.succeeded",
            "mode": "test",
            "data": {
                "refundTicketMerchantExternalId": refund.refund_no,
                "refundStatus": "succeeded",
                "refundedAmount": "10.00",
                "currency": "USD",
                "paymentId": "PAY_1",
            },
        },
        separators=(",", ":"),
    )
    status, result = handle_webhook(
        db, "waffo", {waffo.SIGNATURE_HEADER: _sign_webhook(key, refund_body)}, refund_body.encode(), "x"
    )
    assert (status, result) == (200, "refund_succeeded")
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "refunded"


def test_parse_rejects_missing_key(db, store, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "waffo_webhook_public_key_test", "")
    ch = db.execute(select(PaymentChannel).where(PaymentChannel.code == "waffo")).scalar_one()
    with pytest.raises(SignatureError):
        waffo.WaffoProvider().parse_webhook(ch, {}, b"{}")
