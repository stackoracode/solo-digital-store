"""Operational models: procurement, jobs, manual review, support, audit, settings, AI providers, metrics."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin
from app.models.common import MONEY, JSONType

# Procurement states
PROC_PENDING = "pending"  # created, nothing sent upstream yet
PROC_SUBMITTED = "submitted"  # request sent (or about to be); outcome must be queried, never blindly re-sent
PROC_UNKNOWN = "unknown"  # timeout / ambiguous response; querying upstream
PROC_SUCCEEDED = "succeeded"
PROC_FAILED = "failed"
PROC_NEEDS_REVIEW = "needs_review"
PROC_CANCELED = "canceled"


class Procurement(IdMixin, TimestampMixin, Base):
    __tablename__ = "procurements"

    order_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("order_items.id"), unique=True, nullable=False
    )
    supplier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_sku: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=PROC_PENDING, nullable=False, index=True)
    supplier_order_no: Mapped[str | None] = mapped_column(String(128))
    max_unit_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    quoted_unit_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    actual_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    submit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    query_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Redacted request/response log entries.
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    taken_over_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))


JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_DEAD = "dead"
JOB_CANCELED = "canceled"


class Job(IdMixin, TimestampMixin, Base):
    """Durable job/outbox row. Enqueued in the same DB transaction as the business change."""

    __tablename__ = "jobs"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=JOB_QUEUED, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(80))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)


class ReviewItem(IdMixin, TimestampMixin, Base):
    """Manual review queue. Anything ambiguous lands here instead of being auto-fulfilled."""

    __tablename__ = "review_items"

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), default="high", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payment_attempts.id"))
    procurement_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("procurements.id"))
    transfer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("chain_transfers.id"))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ticket(IdMixin, TimestampMixin, Base):
    __tablename__ = "tickets"

    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    category: Mapped[str] = mapped_column(
        String(16), default="after_sale", nullable=False
    )  # pre_sale|after_sale
    subject: Mapped[str] = mapped_column(String(160), nullable=False)
    # open -> answered -> closed ; customer reply re-opens
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class TicketMessage(IdMixin, Base):
    __tablename__ = "ticket_messages"

    ticket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tickets.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author_type: Mapped[str] = mapped_column(String(16), nullable=False)  # customer | admin | system
    author_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_logs"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    actor_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class Setting(IdMixin, TimestampMixin, Base):
    __tablename__ = "settings"

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONType, nullable=True)

    __table_args__ = (UniqueConstraint("store_id", "key"),)


class AIProvider(IdMixin, TimestampMixin, Base):
    """Configuration only in Phase 1. API keys are encrypted and never rendered back."""

    __tablename__ = "ai_providers"

    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # openai | gemini | anthropic | compatible
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, default="", nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    monthly_budget: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), nullable=False)
    tasks: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class VisitorDaily(IdMixin, Base):
    """Privacy-preserving unique visitor counter: keyed hash of (day, ip, user-agent)."""

    __tablename__ = "visitor_daily"

    day: Mapped[date] = mapped_column(Date, nullable=False)
    visitor_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("day", "visitor_hash"),)


class EmailLog(IdMixin, Base):
    __tablename__ = "email_logs"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    to_email: Mapped[str] = mapped_column(String(254), index=True, nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent | skipped | failed
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
