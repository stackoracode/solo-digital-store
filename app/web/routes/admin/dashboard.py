from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.core.security import decrypt, encrypt, new_totp_secret, totp_uri, verify_totp
from app.db import get_db
from app.models.ops import ReviewItem
from app.models.orders import Order
from app.models.stores import Store
from app.models.users import User
from app.services.audit import audit
from app.services.inventory import stock_report
from app.services.metrics import dashboard
from app.web.deps import Viewer, csrf_form, form_str, get_store, get_viewer, require_admin
from app.web.routes.orders import _qr_data_uri
from app.web.templating import redirect, render

router = APIRouter()


@router.get("")
def admin_home(
    request: Request,
    days: int = 30,
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    days = max(1, min(days, 365))
    stats = dashboard(db, store.id, days)
    reviews = (
        db.execute(
            select(ReviewItem).where(ReviewItem.status == "open").order_by(ReviewItem.id.desc()).limit(10)
        )
        .scalars()
        .all()
    )
    recent = (
        db.execute(select(Order).where(Order.store_id == store.id).order_by(Order.id.desc()).limit(10))
        .scalars()
        .all()
    )
    low_stock = [r for r in stock_report(db, store.id) if r["low"]][:10]
    return render(
        request, "admin/dashboard.html", stats=stats, reviews=reviews, recent=recent, low_stock=low_stock
    )


@router.get("/mfa/setup")
def mfa_setup(request: Request, viewer: Viewer = Depends(get_viewer), db: Session = Depends(get_db)):
    user = viewer.user
    assert user is not None
    if user.totp_enabled:
        return redirect("/admin")
    if not user.totp_secret_enc:
        user.totp_secret_enc = encrypt(new_totp_secret())
        db.commit()
    secret = decrypt(user.totp_secret_enc)
    uri = totp_uri(secret, user.email or f"staff-{user.id}")
    return render(request, "admin/mfa_setup.html", secret=secret, qr=_qr_data_uri(uri), error=None)


@router.post("/mfa/setup")
def mfa_setup_confirm(
    request: Request,
    form: FormData = Depends(csrf_form),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    user = viewer.user
    assert user is not None and viewer.session is not None
    if user.totp_enabled or not user.totp_secret_enc:
        return redirect("/admin")
    secret = decrypt(user.totp_secret_enc)
    if not verify_totp(secret, form_str(form, "code", 10)):
        uri = totp_uri(secret, user.email or f"staff-{user.id}")
        return render(
            request,
            "admin/mfa_setup.html",
            status_code=400,
            secret=secret,
            qr=_qr_data_uri(uri),
            error="code_invalid",
        )
    user.totp_enabled = True
    viewer.session.mfa_passed = True
    audit(db, "mfa_enabled", actor_user_id=user.id, actor_ip=request.state.client_ip)
    db.commit()
    return redirect("/admin")
