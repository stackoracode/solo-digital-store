"""Small read-only JSON API (catalog) for future SPA / agent storefront clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.stores import Store
from app.services.catalog import list_products, min_price
from app.web.deps import get_store, rate_limit

router = APIRouter(prefix="/api/v1")


@router.get("/products")
def api_products(
    request: Request, q: str = "", store: Store = Depends(get_store), db: Session = Depends(get_db)
):
    rate_limit(request, "api_products", 120, 60)
    items = []
    for p in list_products(db, store.id, query=q or None, limit=100):
        price = min_price(p)
        items.append(
            {
                "slug": p.slug,
                "title": p.tr("title", request.state.locale),
                "price": str(price[0]) if price else None,
                "currency": price[1] if price else None,
                "fulfillment_method": p.fulfillment_method,
                "delivery_eta_minutes": p.delivery_eta_minutes,
            }
        )
    return {"items": items}
