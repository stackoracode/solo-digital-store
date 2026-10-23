"""Public storefront: home, categories, search, product detail, checkout, legal pages."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.core.turnstile import verify_turnstile
from app.db import get_db
from app.models.catalog import Category, Product, ProductFormVersion, Sku
from app.models.stores import Store
from app.services import catalog as catalog_svc
from app.services.metrics import record_visit
from app.services.orders import OrderError, create_order
from app.services.settings_store import get_settings_map
from app.web.deps import REF_COOKIE, Viewer, csrf_form, form_str, get_store, get_viewer, rate_limit
from app.web.routes.orders import remember_order
from app.web.templating import redirect, render

router = APIRouter()


def _visit(request: Request, db: Session) -> None:
    try:
        record_visit(db, request.state.client_ip, request.headers.get("user-agent", ""))
    except Exception:  # noqa: BLE001 - metrics must never break the storefront
        db.rollback()


def _cards(db: Session, products: list[Product]) -> list[dict[str, Any]]:
    stock = catalog_svc.stock_by_sku(
        db, [s.id for p in products if p.fulfillment_method == "auto_card" for s in p.skus]
    )
    cards = []
    for p in products:
        price = catalog_svc.min_price(p)
        in_stock = True
        if p.fulfillment_method == "auto_card":
            in_stock = any(stock.get(s.id, 0) > 0 for s in p.active_skus)
        cards.append(
            {
                "product": p,
                "price": price,
                "in_stock": in_stock,
                "image": p.images[0].url if p.images else None,
            }
        )
    return cards


@router.get("/")
def home(
    request: Request,
    q: str = "",
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    _visit(request, db)
    products = catalog_svc.list_products(db, store.id, query=q or None)
    return render(
        request,
        "shop/home.html",
        cards=_cards(db, products),
        categories=catalog_svc.active_categories(db, store.id),
        q=q[:80],
        active_category=None,
    )


@router.get("/c/{slug}")
def category(
    request: Request,
    slug: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    cat = db.execute(
        select(Category).where(
            Category.store_id == store.id, Category.slug == slug[:64], Category.is_active.is_(True)
        )
    ).scalar_one_or_none()
    if cat is None:
        raise HTTPException(404, "Category not found")
    products = catalog_svc.list_products(db, store.id, category_id=cat.id)
    return render(
        request,
        "shop/home.html",
        cards=_cards(db, products),
        categories=catalog_svc.active_categories(db, store.id),
        q="",
        active_category=cat,
    )


def _product_or_404(db: Session, store: Store, slug: str) -> Product:
    product = db.execute(
        select(Product).where(
            Product.store_id == store.id, Product.slug == slug[:80], Product.status == "active"
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(404, "Product not found")
    return product


def _product_page(request: Request, db: Session, store: Store, product: Product, **extra: Any):
    form_fields = []
    if product.current_form_version_id:
        version = db.get(ProductFormVersion, product.current_form_version_id)
        form_fields = version.fields if version else []
    stock = (
        catalog_svc.stock_by_sku(db, [s.id for s in product.active_skus])
        if product.fulfillment_method == "auto_card"
        else {}
    )
    ss = get_settings_map(db, store.id)
    return render(
        request,
        "shop/product.html",
        product=product,
        form_fields=form_fields,
        stock=stock,
        store_settings=ss,
        refund_policy=product.refund_policy or ss["default_refund_policy"],
        **extra,
    )


@router.get("/p/{slug}")
def product_detail(
    request: Request,
    slug: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    _visit(request, db)
    product = _product_or_404(db, store, slug)
    return _product_page(request, db, store, product, errors={}, values={})


@router.post("/p/{slug}/checkout")
def checkout(
    request: Request,
    slug: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "checkout", 20, 600)
    product = _product_or_404(db, store, slug)
    if not verify_turnstile(form_str(form, "cf-turnstile-response", 2048), request.state.client_ip):
        return _product_page(request, db, store, product, errors={"_form": "turnstile"}, values=dict(form))
    try:
        sku_id = int(form_str(form, "sku_id", 20) or "0")
        quantity = int(form_str(form, "quantity", 5) or "1")
    except ValueError:
        return _product_page(request, db, store, product, errors={"_form": "invalid"}, values=dict(form))
    sku = db.get(Sku, sku_id)
    if sku is None or sku.product_id != product.id:
        return _product_page(
            request, db, store, product, errors={"_form": "sku_unavailable"}, values=dict(form)
        )
    user = viewer.user
    email = form_str(form, "email", 254) or (user.email if user and user.email else "")
    try:
        order, token = create_order(
            db,
            store_id=store.id,
            product=product,
            sku=sku,
            quantity=quantity,
            email=email,
            form=form,
            locale=request.state.locale,
            client_ip=request.state.client_ip,
            user=user,
            agent_code=request.cookies.get(REF_COOKIE),
        )
        db.commit()
    except OrderError as exc:
        db.rollback()
        errors = dict(exc.field_errors)
        errors["_form"] = exc.code
        return _product_page(request, db, store, product, errors=errors, values=dict(form))
    response = redirect(f"/o/{order.order_no}?t={token}")
    remember_order(request, response, order.order_no, token)
    return response


@router.get("/legal/{page}")
def legal(
    request: Request,
    page: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    if page not in ("terms", "privacy", "refund"):
        raise HTTPException(404, "Not found")
    return render(request, "legal/page.html", page=page, store_settings=get_settings_map(db, store.id))
