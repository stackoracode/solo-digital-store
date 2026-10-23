from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin


class CardBatch(IdMixin, TimestampMixin, Base):
    __tablename__ = "card_batches"

    sku_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("skus.id"), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    source_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))


class CardCode(IdMixin, TimestampMixin, Base):
    """A single deliverable secret. Stored encrypted; fingerprint (keyed HMAC) enforces de-duplication."""

    __tablename__ = "card_codes"

    sku_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("skus.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("card_batches.id"), index=True)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # available -> reserved(sold) ; available -> void
    status: Mapped[str] = mapped_column(String(16), default="available", nullable=False)
    order_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("order_items.id"), index=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("sku_id", "fingerprint"),
        Index("ix_card_codes_sku_status", "sku_id", "status", "id"),
    )
