"""Admin: products (quick listing + full editor), SKUs, images, order forms, card-code inventory."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from app.core.money import MoneyError, parse_price
from app.core.security import decrypt, mask_secret
from app.db import get_db
from app.models.catalog import FULFILLMENT_METHODS, Category, Product, ProductFormVersion, ProductImage, Sku
from app.models.inventory import CardBatch, CardCode
from app.models.payments import PaymentChannel
from app.models.stores import Store
from app.models.users import User
from app.services import catalog as catalog_svc
from app.services.audit import audit
from app.services.forms import FIELD_TYPES, FormDefinitionError, save_form_version
from app.services.inventory import export_codes_csv, import_codes, stock_report, void_code
from app.services.storage import UploadError, store_public_image
from app.web.deps import csrf_form, form_str, get_store, require_admin, require_owner
from app.web.templating import redirect, render

router = APIRouter()
BLANK_FORM_ROWS = 3


def _product(db: Session, store: Store, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.store_id != store.id:
        raise HTTPException(404, "Product not found")
    return product


@router.get("/products")
def products(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    rows = (
        db.execute(select(Product).where(Product.store_id == store.id).order_by(Product.id.desc()))
        .scalars()
        .all()
    )
    stock = catalog_svc.stock_by_sku(db, [s.id for p in rows for s in p.skus])
    return render(request, "admin/products.html", products=rows, stock=stock)


@router.get("/products/new")
def product_new(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return render(
        request,
        "admin/product_new.html",
        categories=catalog_svc.active_categories(db, store.id),
        methods=FULFILLMENT_METHODS,
        error=None,
        values={},
    )


@router.post("/products/new")
def product_create(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    category_id = form_str(form, "category_id", 20)
    try:
        product = catalog_svc.quick_create_product(
            db,
            store_id=store.id,
            title=form_str(form, "title", 160),
            price=form_str(form, "price", 20),
            description=form_str(form, "description", 20000),
            fulfillment_method=form_str(form, "fulfillment_method", 16),
            category_id=int(category_id) if category_id.isdigit() else None,
            publish=form.get("publish") == "1",
        )
    except catalog_svc.CatalogError as exc:
        db.rollback()
        return render(
            request,
            "admin/product_new.html",
            status_code=400,
            categories=catalog_svc.active_categories(db, store.id),
            methods=FULFILLMENT_METHODS,
            error=str(exc),
            values=dict(form),
        )
    upload = form.get("image")
    if isinstance(upload, UploadFile) and upload.filename:
        try:
            catalog_svc.add_image(db, product, store_public_image(upload.file.read()))
        except (UploadError, catalog_svc.CatalogError):
            pass  # images are optional; the product is still created
    audit(
        db,
        "product_created",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="product",
        target_id=product.id,
    )
    db.commit()
    return redirect(f"/admin/products/{product.id}?created=1")


@router.get("/products/{product_id}")
def product_edit(
    request: Request, product_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)
):
    product = _product(db, store, product_id)
    fields = []
    if product.current_form_version_id:
        version = db.get(ProductFormVersion, product.current_form_version_id)
        fields = version.fields if version else []
    channels = db.execute(select(PaymentChannel).order_by(PaymentChannel.sort)).scalars().all()
    return render(
        request,
        "admin/product_edit.html",
        product=product,
        form_fields=fields,
        blank_rows=range(len(fields), len(fields) + BLANK_FORM_ROWS),
        field_types=FIELD_TYPES,
        categories=catalog_svc.active_categories(db, store.id),
        methods=FULFILLMENT_METHODS,
        channels=channels,
        stock=catalog_svc.stock_by_sku(db, [s.id for s in product.skus]),
        error=request.query_params.get("error"),
        created=request.query_params.get("created"),
    )


def _int(form: FormData, key: str, default: int, lo: int, hi: int) -> int:
    raw = form_str(form, key, 12)
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


@router.post("/products/{product_id}")
def product_update(
    request: Request,
    product_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _product(db, store, product_id)
    title = form_str(form, "title", 160)
    if len(title) < 2:
        return redirect(f"/admin/products/{product.id}?error=title_length")
    method = form_str(form, "fulfillment_method", 16)
    if method not in FULFILLMENT_METHODS:
        return redirect(f"/admin/products/{product.id}?error=fulfillment_method")
    product.title = title
    product.description = form_str(form, "description", 20000)
    product.usage_guide = form_str(form, "usage_guide", 20000)
    product.purchase_notes = form_str(form, "purchase_notes", 20000)
    product.refund_policy = form_str(form, "refund_policy", 5000)
    product.status = (
        form_str(form, "status", 16)
        if form_str(form, "status", 16) in ("draft", "active", "archived")
        else "draft"
    )
    product.fulfillment_method = method
    product.delivery_eta_minutes = _int(form, "delivery_eta_minutes", 0, 0, 60 * 24 * 30)
    product.min_qty = _int(form, "min_qty", 1, 1, 1000)
    product.max_qty = max(product.min_qty, _int(form, "max_qty", 10, 1, 1000))
    product.low_stock_threshold = _int(form, "low_stock_threshold", 5, 0, 100000)
    product.sort = _int(form, "sort", 0, -10000, 10000)
    cat = form_str(form, "category_id", 20)
    category = db.get(Category, int(cat)) if cat.isdigit() else None
    product.category_id = category.id if category is not None and category.store_id == store.id else None
    product.supplier_code = form_str(form, "supplier_code", 40) or None
    product.waffo_product_id = form_str(form, "waffo_product_id", 64) or None
    new_approved = form.get("waffo_approved") == "1"
    if new_approved != product.waffo_approved:
        if user.role != "owner":
            return redirect(f"/admin/products/{product.id}?error=owner_required")
        product.waffo_approved = new_approved
        audit(
            db,
            "waffo_approval_changed",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="product",
            target_id=product.id,
            details={"approved": new_approved},
        )
    channels = [str(c) for c in form.getlist("allowed_channels")]
    product.allowed_channels = channels or None
    zh = {
        k: form_str(form, f"zh_{k}", 20000) for k in ("title", "description", "usage_guide", "purchase_notes")
    }
    product.translations = {"zh-CN": {k: v for k, v in zh.items() if v}} if any(zh.values()) else {}
    product.slug = catalog_svc.unique_slug(
        db, store.id, form_str(form, "slug", 80) or title, exclude_id=product.id
    )

    raw_fields = []
    for i in range(0, 40):
        if f"f{i}_key" not in form:
            continue
        options = form_str(form, f"f{i}_options", 4000).replace("\r", "").replace(",", "\n").split("\n")
        raw_fields.append(
            {
                "key": form_str(form, f"f{i}_key", 40),
                "label": form_str(form, f"f{i}_label", 80),
                "type": form_str(form, f"f{i}_type", 16),
                "help": form_str(form, f"f{i}_help", 200),
                "required": form.get(f"f{i}_required") == "1",
                "max_length": form_str(form, f"f{i}_max_length", 6) or None,
                "pattern": form_str(form, f"f{i}_pattern", 200),
                "options": [o.strip() for o in options if o.strip()],
                "order": _int(form, f"f{i}_order", i, 0, 1000),
                "min": form_str(form, f"f{i}_min", 30),
                "max": form_str(form, f"f{i}_max", 30),
            }
        )
    try:
        save_form_version(db, product, raw_fields)
    except FormDefinitionError as exc:
        db.rollback()
        return redirect(f"/admin/products/{product.id}?error=" + str(exc)[:150])
    audit(
        db,
        "product_updated",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="product",
        target_id=product.id,
    )
    db.commit()
    return redirect(f"/admin/products/{product.id}")


@router.post("/products/{product_id}/skus")
def sku_save(
    request: Request,
    product_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _product(db, store, product_id)
    sku_id = form_str(form, "sku_id", 20)
    sku = db.get(Sku, int(sku_id)) if sku_id.isdigit() else None
    if sku is not None and sku.product_id != product.id:
        raise HTTPException(404, "SKU not found")
    currency = (form_str(form, "currency", 8) or "USD").upper()
    try:
        price = parse_price(form_str(form, "price", 20), currency)
        cap_raw = form_str(form, "max_unit_cost", 20)
        cap = parse_price(cap_raw, currency) if cap_raw else None
    except MoneyError:
        return redirect(f"/admin/products/{product.id}?error=price")
    if sku is None:
        sku = Sku(product_id=product.id, price=price, currency=currency)
        db.add(sku)
    sku.name = form_str(form, "name", 120) or "Default"
    sku.price = price
    sku.currency = currency
    sku.is_active = form.get("is_active") == "1"
    sku.supplier_sku = form_str(form, "supplier_sku", 80) or None
    sku.max_unit_cost = cap
    sku.sort = _int(form, "sort", 0, -1000, 1000)
    audit(
        db,
        "sku_saved",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="product",
        target_id=product.id,
        details={"sku": sku.name, "price": str(price), "currency": currency},
    )
    db.commit()
    return redirect(f"/admin/products/{product.id}#skus")


@router.post("/products/{product_id}/images")
def image_add(
    request: Request,
    product_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _product(db, store, product_id)
    try:
        upload = form.get("image")
        if isinstance(upload, UploadFile) and upload.filename:
            url = store_public_image(upload.file.read())
        else:
            url = form_str(form, "url", 500)
        catalog_svc.add_image(db, product, url, form_str(form, "alt", 160))
    except (UploadError, catalog_svc.CatalogError) as exc:
        db.rollback()
        return redirect(f"/admin/products/{product.id}?error=image_{exc}")
    db.commit()
    return redirect(f"/admin/products/{product.id}#images")


@router.post("/products/{product_id}/images/{image_id}/delete")
def image_delete(
    request: Request,
    product_id: int,
    image_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    product = _product(db, store, product_id)
    image = db.get(ProductImage, image_id)
    if image is not None and image.product_id == product.id:
        db.delete(image)
        db.commit()
    return redirect(f"/admin/products/{product.id}#images")


# Inventory --------------------------------------------------------------------------------------


@router.get("/inventory")
def inventory(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return render(request, "admin/inventory.html", report=stock_report(db, store.id))


def _sku(db: Session, store: Store, sku_id: int) -> tuple[Sku, Product]:
    sku = db.get(Sku, sku_id)
    product = db.get(Product, sku.product_id) if sku else None
    if sku is None or product is None or product.store_id != store.id:
        raise HTTPException(404, "SKU not found")
    return sku, product


@router.get("/skus/{sku_id}/codes")
def sku_codes(
    request: Request, sku_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)
):
    sku, product = _sku(db, store, sku_id)
    batches = (
        db.execute(select(CardBatch).where(CardBatch.sku_id == sku.id).order_by(CardBatch.id.desc()))
        .scalars()
        .all()
    )
    counts: dict[str, int] = {
        status: int(n)
        for status, n in db.execute(
            select(CardCode.status, func.count()).where(CardCode.sku_id == sku.id).group_by(CardCode.status)
        ).all()
    }
    recent = (
        db.execute(select(CardCode).where(CardCode.sku_id == sku.id).order_by(CardCode.id.desc()).limit(50))
        .scalars()
        .all()
    )
    masked = [
        {
            "id": c.id,
            "status": c.status,
            "batch_id": c.batch_id,
            "masked": mask_secret(decrypt(c.secret_enc)),
            "sold_at": c.sold_at,
        }
        for c in recent
    ]
    return render(
        request,
        "admin/sku_codes.html",
        sku=sku,
        product=product,
        batches=batches,
        counts=counts,
        codes=masked,
        result=request.query_params.get("result"),
    )


@router.post("/skus/{sku_id}/codes/import")
def sku_codes_import(
    request: Request,
    sku_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sku, _ = _sku(db, store, sku_id)
    text = form_str(form, "codes", 2_000_000)
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        raw = upload.file.read(2_000_001)
        if len(raw) > 2_000_000:
            return redirect(f"/admin/skus/{sku.id}/codes?result=file_too_large")
        try:
            text += "\n" + raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return redirect(f"/admin/skus/{sku.id}/codes?result=not_utf8")
    try:
        result = import_codes(db, sku, text, label=form_str(form, "label", 120), user_id=user.id)
    except ValueError as exc:
        db.rollback()
        return redirect(f"/admin/skus/{sku.id}/codes?result={str(exc)[:80]}")
    audit(
        db,
        "codes_imported",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="sku",
        target_id=sku.id,
        details={
            "imported": result.imported,
            "duplicates": result.duplicates,
            "rejected": result.rejected,
            "batch": result.batch_id,
        },
    )
    db.commit()
    return redirect(
        f"/admin/skus/{sku.id}/codes?result=imported:{result.imported},duplicates:{result.duplicates},"
        f"rejected:{result.rejected}"
    )


@router.get("/skus/{sku_id}/codes/export")
def sku_codes_export(
    request: Request,
    sku_id: int,
    status: str = "available",
    store: Store = Depends(get_store),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    sku, _ = _sku(db, store, sku_id)
    if status not in ("available", "sold", "void"):
        raise HTTPException(400, "Invalid status")
    data = export_codes_csv(db, sku.id, status)
    audit(
        db,
        "codes_exported",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="sku",
        target_id=sku.id,
        details={"status": status},
    )
    db.commit()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    return Response(
        data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="sku{sku.id}-{status}-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/codes/{code_id}/void")
def code_void(
    request: Request,
    code_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    code = db.execute(select(CardCode).where(CardCode.id == code_id).with_for_update()).scalar_one_or_none()
    if code is None:
        raise HTTPException(404, "Code not found")
    _sku(db, store, code.sku_id)
    if void_code(db, code):
        audit(
            db,
            "code_voided",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="card_code",
            target_id=code.id,
        )
    db.commit()
    return redirect(f"/admin/skus/{code.sku_id}/codes")
