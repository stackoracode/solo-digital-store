"""Orders keep three independent status axes: payment, fulfilment and after-sale."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, IdMixin, TimestampMixin
from app.models.common import MONEY, JSONType

# Payment axis
PAY_UNPAID = "unpaid"
PAY_PAID = "paid"
PAY_REFUND_PENDING = "refund_pending"
PAY_PARTIALLY_REFUNDED = "partially_refunded"
PAY_REFUNDED = "refunded"

# Fulfilment axis (order level is an aggregate of items)
FUL_NOT_STARTED = "not_started"
FUL_QUEUED = "queued"
FUL_PROCESSING = "processing"
FUL_AWAITING_STOCK = "awaiting_stock"
FUL_AWAITING_MANUAL = "awaiting_manual"
FUL_AWAITING_SUPPLIER = "awaiting_supplier"
FUL_NEEDS_REVIEW = "needs_review"
FUL_DELIVERED = "delivered"
FUL_CANCELED = "canceled"

# After-sale axis
AS_NONE = "none"
AS_OPEN = "open"
AS_RESOLVED = "resolved"

# Lifecycle
LC_PENDING_PAYMENT = "pending_payment"
LC_ACTIVE = "active"
LC_COMPLETED = "completed"
LC_EXPIRED = "expired"
LC_CANCELED = "canceled"


class Order(IdMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), default="en-US", nullable=False)
    # SHA-256 of the secret order access token handed to the buyer (never stored in clear).
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    refunded_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    fee_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    cost_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)

    lifecycle: Mapped[str] = mapped_column(String(20), default=LC_PENDING_PAYMENT, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(20), default=PAY_UNPAID, nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(String(20), default=FUL_NOT_STARTED, nullable=False)
    after_sale_status: Mapped[str] = mapped_column(String(20), default=AS_NONE, nullable=False)

    paid_attempt_id: Mapped[int | None] = mapped_column(BigInteger)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    admin_note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", order_by="OrderItem.id", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_orders_store_created", "store_id", "created_at"),
        Index("ix_orders_email", "email"),
        Index("ix_orders_status", "payment_status", "fulfillment_status"),
    )


class OrderItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True, nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("skus.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fulfillment_method: Mapped[str] = mapped_column(String(16), nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(String(20), default="not_started", nullable=False)
    delivered_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    form_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("product_form_versions.id"))
    form_data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    # Product/price/fulfilment rules as shown to the buyer at order time.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class Delivery(IdMixin, TimestampMixin, Base):
    """One delivered unit of content. card_code_id is unique: a code can never be delivered twice."""

    __tablename__ = "deliveries"

    order_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("order_items.id"), index=True, nullable=False
    )
    # card_code | manual | supplier
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    card_code_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("card_codes.id"), unique=True)
    procurement_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("procurements.id"))
    content_enc: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderEvent(IdMixin, Base):
    """Append-only order timeline (status transitions, admin actions, notes)."""

    __tablename__ = "order_events"

    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    visible_to_customer: Mapped[bool] = mapped_column(default=False, nullable=False)


class Refund(IdMixin, TimestampMixin, Base):
    __tablename__ = "refunds"

    refund_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True, nullable=False)
    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("payment_attempts.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # requested -> processing -> succeeded | failed ; manual_required (e.g. on-chain refunds)
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    requested_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fee_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)

    __table_args__ = (UniqueConstraint("provider_ref"),)
