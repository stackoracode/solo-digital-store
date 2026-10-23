"""Store / agent / domain model. Phase 1 has one self-operated store; agents only earn commission."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin
from app.models.common import MONEY


class Store(IdMixin, TimestampMixin, Base):
    __tablename__ = "stores"

    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Future: agent storefronts. Payments are still collected by the self-operated entity.
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id", use_alter=True))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class StoreDomain(IdMixin, TimestampMixin, Base):
    __tablename__ = "store_domains"

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), index=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    # pending -> verified -> disabled. Only verified domains are routed.
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    verification_token: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cf_custom_hostname_id: Mapped[str | None] = mapped_column(String(64))


class Agent(IdMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    # applied -> active | rejected; active -> suspended
    status: Mapped[str] = mapped_column(String(16), default="applied", nullable=False)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.05"), nullable=False)
    application_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    admin_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Commission(IdMixin, TimestampMixin, Base):
    __tablename__ = "commissions"

    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), unique=True, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agents.id"), index=True, nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    # pending -> approved -> paid; any non-paid state -> reversed (refund)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    reversed_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
