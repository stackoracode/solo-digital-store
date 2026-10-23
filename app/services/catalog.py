"""Catalog: quick listing (Xianyu-style), product editing, search, stock counts."""

from __future__ import annotations

import re
import secrets
import unicodedata
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.money import parse_price
from app.models.catalog import FULFILLMENT_METHODS, Category, Product, ProductImage, Sku
from app.models.inventory import CardCode


class CatalogError(ValueError):
    pass


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")[:60]
    return slug or "item"


def unique_slug(db: Session, store_id: int, base: str, exclude_id: int | None = None) -> str:
    slug = slugify(base)
    candidate = slug
    while True:
        q = select(Product.id).where(Product.store_id == store_id, Product.slug == candidate)
        if exclude_id:
            q = q.where(Product.id != exclude_id)
        if db.scalar(q) is None:
            return candidate
        candidate = f"{slug}-{secrets.token_hex(2)}"


def quick_create_product(
    db: Session,
    *,
    store_id: int,
    title: str,
    price: str,
    description: str,
    fulfillment_method: str,
    currency: str = "USD",
    category_id: int | None = None,
    publish: bool = True,
) -> Product:
    """Minimal "list an item" flow: name, price, description, delivery method. Images optional."""
    title = title.strip()
    if not 2 <= len(title) <= 160:
        raise CatalogError("title_length")
    if fulfillment_method not in FULFILLMENT_METHODS:
        raise CatalogError("fulfillment_method")
    try:
        amount = parse_price(price, currency)
    except ValueError as exc:
        raise CatalogError("price") from exc
    product = Product(
        store_id=store_id,
        category_id=category_id,
        slug=unique_slug(db, store_id, title),
        title=title,
        description=description.strip()[:20000],
        fulfillment_method=fulfillment_method,
        delivery_eta_minutes=0 if fulfillment_method == "auto_card" else 60,
        status="active" if publish else "draft",
        translations={},
    )
    db.add(product)
    db.flush()
    db.add(Sku(product_id=product.id, name="Default", price=amount, currency=currency))
    db.flush()
    db.refresh(product)
    return product


def add_image(db: Session, product: Product, url: str, alt: str = "") -> ProductImage:
    if not re.match(r"^(https://|/media/)", url) or len(url) > 500:
        raise CatalogError("image_url")
    image = ProductImage(product_id=product.id, url=url, alt=alt[:160], sort=len(product.images))
    db.add(image)
    return image


def list_products(
    db: Session,
    store_id: int,
    *,
    query: str | None = None,
    category_id: int | None = None,
    limit: int = 48,
    offset: int = 0,
) -> list[Product]:
    stmt = select(Product).where(Product.store_id == store_id, Product.status == "active")
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if query:
        term = query.strip()[:80]
        like = f"%{term.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(Product.title.ilike(like), Product.description.ilike(like)))
    stmt = stmt.order_by(Product.sort, Product.id.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def active_categories(db: Session, store_id: int) -> list[Category]:
    return list(
        db.execute(
            select(Category)
            .where(Category.store_id == store_id, Category.is_active.is_(True))
            .order_by(Category.sort)
        ).scalars()
    )


def stock_by_sku(db: Session, sku_ids: list[int]) -> dict[int, int]:
    if not sku_ids:
        return {}
    rows = db.execute(
        select(CardCode.sku_id, func.count())
        .where(CardCode.sku_id.in_(sku_ids), CardCode.status == "available")
        .group_by(CardCode.sku_id)
    ).all()
    data = dict.fromkeys(sku_ids, 0)
    data.update({sku_id: int(n) for sku_id, n in rows})
    return data


def min_price(product: Product) -> tuple[Decimal, str] | None:
    skus = product.active_skus
    if not skus:
        return None
    cheapest = min(skus, key=lambda s: s.price)
    return cheapest.price, cheapest.currency
