from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin
from app.models.common import MONEY, TOKEN_UNITS, JSONType

ATT_CREATED = "created"
ATT_PENDING = "pending"
ATT_SUCCEEDED = "succeeded"
ATT_FAILED = "failed"
ATT_EXPIRED = "expired"
ATT_CANCELED = "canceled"
ATT_NEEDS_REVIEW = "needs_review"
ACTIVE_ATTEMPT_STATES = (ATT_CREATED, ATT_PENDING)

CRYPTO_ADDRESS_SEQ = Sequence("crypto_address_index_seq", metadata=Base.metadata, start=1)


class PaymentChannel(IdMixin, TimestampMixin, Base):
    """Admin-configurable channel. Secrets are never stored here; they come from env."""

    __tablename__ = "payment_channels"

    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # mock | waffo | usdt
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), default="test", nullable=False)  # test | live
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0"), nullable=False)
    fee_fixed: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    min_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    max_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentAttempt(IdMixin, TimestampMixin, Base):
    """A standardized payment order against one channel. Unique per (order, channel) while active."""

    __tablename__ = "payment_attempts"

    attempt_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True, nullable=False)
    channel_code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ATT_CREATED, nullable=False)
    # Snapshot of the order amount at creation.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # What the payer must actually send (e.g. USDT amount after FX).
    pay_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    pay_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    received_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    fx_source: Mapped[str | None] = mapped_column(String(40))
    fx_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_ref: Mapped[str | None] = mapped_column(String(128))
    provider_payment_id: Mapped[str | None] = mapped_column(String(128))
    checkout_url: Mapped[str | None] = mapped_column(String(1000))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    refunded_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)

    __table_args__ = (
        Index(
            "uq_payment_attempts_active_per_channel",
            "order_id",
            "channel_code",
            unique=True,
            postgresql_where=text("status IN ('created', 'pending')"),
        ),
        UniqueConstraint("provider", "provider_payment_id"),
        Index("ix_payment_attempts_status", "status", "expires_at"),
    )


class PaymentEvent(IdMixin, Base):
    """Every inbound provider notification, verified or not. (provider, event_type, event_id) dedupes."""

    __tablename__ = "payment_events"

    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(128))
    attempt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payment_attempts.id"), index=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(40), default="received", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    remote_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (UniqueConstraint("provider", "event_type", "event_id"),)


class CryptoAddress(IdMixin, TimestampMixin, Base):
    """Watch-only deposit address derived from the merchant xpub. Never reused across attempts."""

    __tablename__ = "crypto_addresses"

    network: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False)  # lowercase 0x...
    derivation_index: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("payment_attempts.id"), unique=True)
    watch_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChainCursor(IdMixin, TimestampMixin, Base):
    __tablename__ = "chain_cursors"

    network: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    last_scanned_block: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_head_block: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


TX_SEEN = "seen"
TX_CONFIRMED = "confirmed"
TX_CREDITED = "credited"
TX_FLAGGED = "flagged"
TX_REORGED = "reorged"


class ChainTransfer(IdMixin, TimestampMixin, Base):
    """An ERC-20 Transfer log observed on-chain to one of our deposit addresses."""

    __tablename__ = "chain_transfers"

    network: Mapped[str] = mapped_column(String(32), nullable=False)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_contract: Mapped[str] = mapped_column(String(42), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    from_address: Mapped[str] = mapped_column(String(42), nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    amount_units: Mapped[Decimal] = mapped_column(TOKEN_UNITS, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TX_SEEN, nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payment_attempts.id"), index=True)
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    missing_checks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("chain_id", "tx_hash", "log_index"),
        Index("ix_chain_transfers_status", "network", "status"),
    )
