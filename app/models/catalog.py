from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, IdMixin, TimestampMixin
from app.models.common import MONEY, JSONType

FULFILLMENT_METHODS = ("auto_card", "manual", "supplier")


class Category(IdMixin, TimestampMixin, Base):
    __tablename__ = "categories"

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    translations: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("store_id", "slug"),)


class Product(IdMixin, TimestampMixin, Base):
    __tablename__ = "products"

    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stores.id"), index=True, nullable=False)
    category_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("categories.id"), index=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    usage_guide: Mapped[str] = mapped_column(Text, default="", nullable=False)
    purchase_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    refund_policy: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # {"zh-CN": {"title": "...", "description": "...", ...}}
    translations: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    # draft | active | archived
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False, index=True)
    fulfillment_method: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    delivery_eta_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_qty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_qty: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Supplier fulfilment config (used when fulfillment_method == "supplier").
    supplier_code: Mapped[str | None] = mapped_column(String(40))
    # Payment compliance: Waffo product mapping + explicit merchant-review approval flag.
    waffo_product_id: Mapped[str | None] = mapped_column(String(64))
    waffo_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # None = every enabled channel; otherwise list of channel codes.
    allowed_channels: Mapped[list[str] | None] = mapped_column(JSONType)
    current_form_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_form_versions.id", use_alter=True)
    )

    skus: Mapped[list[Sku]] = relationship(
        back_populates="product", order_by="Sku.sort, Sku.id", lazy="selectin"
    )
    images: Mapped[list[ProductImage]] = relationship(
        order_by="ProductImage.sort, ProductImage.id", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("store_id", "slug"),)

    def tr(self, field: str, locale: str) -> str:
        value = (self.translations or {}).get(locale, {}).get(field)
        return value or getattr(self, field)

    @property
    def active_skus(self) -> list[Sku]:
        return [s for s in self.skus if s.is_active]


class Sku(IdMixin, TimestampMixin, Base):
    __tablename__ = "skus"

    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="Default", nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    compare_at_price: Mapped[Decimal | None] = mapped_column(MONEY)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    supplier_sku: Mapped[str | None] = mapped_column(String(80))
    # Procurement price cap per unit; supplier quotes above this go to manual review.
    max_unit_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    unit_cost: Mapped[Decimal | None] = mapped_column(MONEY)

    product: Mapped[Product] = relationship(back_populates="skus")


class ProductImage(IdMixin, TimestampMixin, Base):
    __tablename__ = "product_images"

    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    alt: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProductFormVersion(IdMixin, TimestampMixin, Base):
    """Immutable snapshot of a product's custom order form. Editing creates a new version."""

    __tablename__ = "product_form_versions"

    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)

    __table_args__ = (UniqueConstraint("product_id", "version"),)
