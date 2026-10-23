"""Admin: manual review queue, jobs (incl. dead letters), procurements, audit log."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.jobs.queue import retry_dead
from app.models.ops import AuditLog, Job, Procurement, ReviewItem
from app.models.payments import PaymentAttempt
from app.models.stores import Store
from app.models.users import User
from app.services.audit import audit
from app.services.payments import apply_payment_success
from app.services.procurement import admin_take_over
from app.services.review import resolve_review
from app.web.deps import csrf_form, form_str, get_store, require_admin, require_owner
from app.web.templating import redirect, render

router = APIRouter()
CREDITABLE = ("late_payment", "late_transfer", "underpaid", "waffo_missing_webhook", "balance_mismatch")


@router.get("/reviews")
def reviews(request: Request, status: str = "open", db: Session = Depends(get_db)):
    stmt = select(ReviewItem).order_by(ReviewItem.id.desc()).limit(200)
    if status in ("open", "resolved", "dismissed"):
        stmt = stmt.where(ReviewItem.status == status)
    return render(
        request,
        "admin/reviews.html",
        items=db.execute(stmt).scalars().all(),
        status=status,
        creditable=CREDITABLE,
    )


@router.post("/reviews/{item_id}/resolve")
def review_resolve(
    request: Request,
    item_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.get(ReviewItem, item_id, with_for_update=True)
    if item is None:
        raise HTTPException(404, "Not found")
    action = form_str(form, "action", 20)
    resolve_review(
        db,
        item,
        user.id,
        form_str(form, "resolution", 2000) or action,
        status="dismissed" if action == "dismiss" else "resolved",
    )
    audit(
        db,
        f"review_{action or 'resolve'}",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="review",
        target_id=item.id,
    )
    db.commit()
    return redirect("/admin/reviews")


@router.post("/reviews/{item_id}/credit")
def review_credit(
    request: Request,
    item_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Owner confirms money was really received (checked on explorer / provider dashboard)."""
    item = db.get(ReviewItem, item_id, with_for_update=True)
    if item is None or item.kind not in CREDITABLE or item.attempt_id is None or item.status != "open":
        raise HTTPException(400, "Not creditable")
    attempt = db.get(PaymentAttempt, item.attempt_id)
    assert attempt is not None
    if form.get("confirm") != "1":
        raise HTTPException(400, "Confirmation required")
    actually_received = attempt.received_amount
    if attempt.status == "needs_review":
        attempt.status = "pending"
    # The owner accepts the payment as settling the order; the real received amount is kept for audit.
    result = apply_payment_success(
        db,
        attempt.id,
        amount=attempt.pay_amount,
        currency=attempt.pay_currency,
        provider_payment_id=attempt.provider_payment_id,
        source=f"manual_review:{item.id}",
        allow_late=True,
        actor_user_id=user.id,
    )
    if attempt.provider == "usdt":
        attempt.received_amount = actually_received
    resolve_review(db, item, user.id, f"credited manually ({result}); " + form_str(form, "resolution", 1500))
    audit(
        db,
        "payment_credited_manually",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="attempt",
        target_id=attempt.id,
        details={"review": item.id, "result": result},
    )
    db.commit()
    return redirect("/admin/reviews")


@router.get("/jobs")
def jobs(request: Request, status: str = "dead", db: Session = Depends(get_db)):
    stmt = select(Job).order_by(Job.id.desc()).limit(200)
    if status in ("queued", "running", "succeeded", "dead", "canceled"):
        stmt = stmt.where(Job.status == status)
    return render(request, "admin/jobs.html", jobs=db.execute(stmt).scalars().all(), status=status)


@router.post("/jobs/{job_id}/retry")
def job_retry(
    request: Request,
    job_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if retry_dead(db, job_id):
        audit(
            db,
            "job_retried",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="job",
            target_id=job_id,
        )
        db.commit()
    return redirect("/admin/jobs")


@router.get("/procurements")
def procurements(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(select(Procurement).order_by(Procurement.id.desc()).limit(200)).scalars().all()
    return render(request, "admin/procurements.html", procurements=rows)


@router.post("/procurements/{proc_id}/take-over")
def procurement_take_over(
    request: Request,
    proc_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    admin_take_over(db, proc_id, user.id, form_str(form, "note", 1000))
    audit(
        db,
        "procurement_taken_over",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="procurement",
        target_id=proc_id,
    )
    db.commit()
    return redirect("/admin/procurements")


@router.get("/audit")
def audit_log(request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).scalars().all()
    return render(request, "admin/audit.html", rows=rows)
