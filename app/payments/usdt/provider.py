"""USDT (EVM, read-only) payment adapter: unique watch-only address per attempt, exact FX snapshot."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.core.money import q_up
from app.db import utcnow
from app.models.payments import CRYPTO_ADDRESS_SEQ, CryptoAddress
from app.payments.base import CreateResult, ProviderError, RefundResult, SignatureError
from app.payments.usdt.hd import XpubError, derive_address
from app.payments.usdt.networks import get_network

# Late transfers to an address stay attributable (and reviewable) for this long.
WATCH_DAYS = 30
RATE_SANITY = (Decimal("0.97"), Decimal("1.03"))


def usd_per_usdt() -> tuple[Decimal, str]:
    source = get_settings().usdt_rate_source
    if source == "peg":
        return Decimal("1"), "peg:1USD=1USDT"
    try:
        resp = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": "usd"},
            timeout=5.0,
        )
        price = Decimal(str(resp.json()["tether"]["usd"]))
    except (httpx.HTTPError, KeyError, ValueError, ArithmeticError) as exc:
        raise ProviderError("USDT rate source unavailable") from exc
    if not RATE_SANITY[0] <= price <= RATE_SANITY[1]:
        raise ProviderError(f"USDT rate {price} outside sanity band; refusing to quote", retryable=False)
    return price, "coingecko:tether/usd"


class UsdtProvider:
    name = "usdt"

    def _network(self, channel):  # type: ignore[no-untyped-def]
        return get_network(channel.config.get("network", ""))

    def check_available(self, channel, order):  # type: ignore[no-untyped-def]
        settings = get_settings()
        if not settings.usdt_xpub:
            return "not_configured"
        try:
            self._network(channel)
        except KeyError:
            return "network_unknown"
        if order.currency != "USD":
            return "currency_unsupported"
        if channel.mode == "live" and (not settings.usdt_live_enabled or channel.verified_at is None):
            return "live_disabled"
        return None

    def create_payment(self, db, channel, order, attempt):  # type: ignore[no-untyped-def]
        network = self._network(channel)
        rate, source = usd_per_usdt()
        # Exact conversion rounded up to whole cents. No artificial "unique decimals" are added:
        # the per-attempt address is what links a transfer to an order.
        pay_amount = q_up(attempt.amount / rate, "USD")
        index = int(db.scalar(select(CRYPTO_ADDRESS_SEQ.next_value())))
        try:
            address = derive_address(get_settings().usdt_xpub, index)
        except XpubError as exc:
            raise ProviderError(str(exc), retryable=False, code="xpub") from exc
        db.add(
            CryptoAddress(
                network=network.key,
                address=address,
                derivation_index=index,
                attempt_id=attempt.id,
                watch_until=utcnow() + timedelta(days=WATCH_DAYS),
            )
        )
        return CreateResult(
            pay_amount=pay_amount,
            pay_currency="USDT",
            expires_at=utcnow() + timedelta(minutes=get_settings().payment_attempt_ttl_minutes),
            provider_ref=address,
            checkout_url=None,
            fx_rate=rate,
            fx_source=source,
            extra={
                "network": network.key,
                "network_name": network.display_name,
                "chain_id": network.chain_id,
                "token_symbol": network.token_symbol,
                "token_contract": network.token_contract,
                "address": address,
                "derivation_index": index,
                "confirmations": network.confirmations,
                "explorer_address_url": network.address_url(address),
            },
        )

    def parse_webhook(self, channel, headers, body):  # type: ignore[no-untyped-def]
        raise SignatureError("USDT payments are verified on-chain only; webhooks are not accepted")

    def refund(self, channel, attempt, refund):  # type: ignore[no-untyped-def]
        # Outbound transfers require keys the server never holds: an operator refunds manually and
        # records the transaction hash.
        return RefundResult(status="manual_required")
