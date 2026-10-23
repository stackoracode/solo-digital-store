"""Admin: payment channels (enable/disable/configure/monitor), attempts, events, on-chain transfers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.db import get_db, utcnow
from app.models.payments import ChainCursor, ChainTransfer, PaymentAttempt, PaymentChannel, PaymentEvent
from app.models.users import User
from app.payments.usdt.networks import get_network, load_networks
from app.payments.usdt.rpc import JsonRpcReader, RpcError
from app.payments.usdt.scanner import verify_network
from app.services.audit import audit
from app.web.deps import csrf_form, form_str, require_admin, require_owner
from app.web.templating import redirect, render

router = APIRouter()


@router.get("/payments")
def channels(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(select(PaymentChannel).order_by(PaymentChannel.sort, PaymentChannel.id)).scalars().all()
    since = utcnow() - timedelta(hours=24)
    stats = {}
    for ch in rows:
        counts = dict(
            db.execute(
                select(PaymentAttempt.status, func.count())
                .where(PaymentAttempt.channel_code == ch.code, PaymentAttempt.created_at >= since)
                .group_by(PaymentAttempt.status)
            ).all()
        )
        stats[ch.code] = counts
    cursors = {c.network: c for c in db.execute(select(ChainCursor)).scalars()}
    s = get_settings()
    secrets_status = {
        "waffo": bool(s.waffo_merchant_id and s.waffo_private_key),
        "waffo_webhook_test": bool(s.waffo_webhook_public_key_test),
        "waffo_webhook_prod": bool(s.waffo_webhook_public_key_prod),
        "waffo_live_enabled": s.waffo_live_enabled,
        "usdt_xpub": bool(s.usdt_xpub),
        "usdt_live_enabled": s.usdt_live_enabled,
        "mock_allowed": s.mock_allowed,
    }
    return render(
        request,
        "admin/payments.html",
        channels=rows,
        stats=stats,
        cursors=cursors,
        networks=load_networks(),
        secrets_status=secrets_status,
        webhook_base=f"{s.public_base_url.rstrip('/')}/webhooks/",
        error=request.query_params.get("error"),
    )


def _decimal(raw: str, default: Decimal | None) -> Decimal | None:
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return default
    return value if value.is_finite() and value >= 0 else default


@router.post("/payments/{code}")
def channel_update(
    request: Request,
    code: str,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    ch = db.execute(select(PaymentChannel).where(PaymentChannel.code == code)).scalar_one_or_none()
    if ch is None:
        raise HTTPException(404, "Channel not found")
    before = {"enabled": ch.enabled, "mode": ch.mode}
    mode = form_str(form, "mode", 8)
    if mode == "live" and ch.provider == "mock":
        return redirect("/admin/payments?error=mock_cannot_be_live")
    if mode == "live" and ch.provider == "usdt" and ch.verified_at is None:
        return redirect("/admin/payments?error=verify_network_first")
    ch.mode = mode if mode in ("test", "live") else ch.mode
    ch.enabled = form.get("enabled") == "1"
    ch.display_name = form_str(form, "display_name", 80) or ch.display_name
    sort_raw = form_str(form, "sort", 6)
    if sort_raw.lstrip("-").isdigit():
        ch.sort = int(sort_raw)
    ch.fee_percent = _decimal(form_str(form, "fee_percent", 12), ch.fee_percent) or Decimal("0")
    ch.fee_fixed = _decimal(form_str(form, "fee_fixed", 12), ch.fee_fixed) or Decimal("0")
    ch.min_amount = _decimal(form_str(form, "min_amount", 20), None)
    ch.max_amount = _decimal(form_str(form, "max_amount", 20), None)
    if ch.provider == "waffo":
        ch.config = {**ch.config, "tax_category": form_str(form, "tax_category", 40) or "digital_goods"}
    audit(
        db,
        "payment_channel_updated",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="channel",
        target_id=ch.code,
        details={"before": before, "after": {"enabled": ch.enabled, "mode": ch.mode}},
    )
    db.commit()
    return redirect("/admin/payments")


@router.post("/payments/{code}/verify")
def channel_verify(
    request: Request,
    code: str,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """USDT: confirm RPC chain id and token decimals match config before the network can go live."""
    ch = db.execute(select(PaymentChannel).where(PaymentChannel.code == code)).scalar_one_or_none()
    if ch is None or ch.provider != "usdt":
        raise HTTPException(404, "Channel not found")
    network = get_network(ch.config.get("network", ""))
    try:
        verify_network(JsonRpcReader(network), network)
    except RpcError as exc:
        ch.verified_at = None
        ch.last_error = str(exc)[:1000]
        db.commit()
        return redirect("/admin/payments?error=verify_failed")
    ch.verified_at = utcnow()
    ch.last_error = ""
    audit(
        db,
        "usdt_network_verified",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="channel",
        target_id=ch.code,
    )
    db.commit()
    return redirect("/admin/payments")


@router.post("/payments/cursor/{network}/rescan")
def cursor_rescan(
    request: Request,
    network: str,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    cursor = db.execute(
        select(ChainCursor).where(ChainCursor.network == network).with_for_update()
    ).scalar_one_or_none()
    if cursor is None:
        raise HTTPException(404, "Cursor not found")
    raw = form_str(form, "from_block", 20)
    if raw.isdigit():
        cursor.last_scanned_block = int(raw)
        audit(
            db,
            "usdt_rescan",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="cursor",
            target_id=network,
            details={"from_block": raw},
        )
        db.commit()
    return redirect("/admin/payments")


@router.get("/payments/attempts")
def attempts(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(PaymentAttempt).order_by(PaymentAttempt.id.desc()).limit(200)).scalars().all()
    return render(request, "admin/attempts.html", attempts=rows)


@router.get("/payments/events")
def events(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(PaymentEvent).order_by(PaymentEvent.id.desc()).limit(200)).scalars().all()
    return render(request, "admin/events.html", events=rows)


@router.get("/payments/transfers")
def transfers(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(ChainTransfer).order_by(ChainTransfer.id.desc()).limit(200)).scalars().all()
    nets = load_networks()
    return render(request, "admin/transfers.html", transfers=rows, networks=nets)
