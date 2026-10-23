"""Order pages: status, payment selection, USDT instructions, secure lookup, after-sale tickets."""

from __future__ import annotations

import base64
import io
import secrets

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.core.turnstile import verify_turnstile
from app.db import get_db, utcnow
from app.jobs.queue import enqueue
from app.models.orders import Order, OrderEvent
from app.models.payments import ACTIVE_ATTEMPT_STATES, ChainTransfer, PaymentAttempt
from app.models.stores import Store
from app.payments import mock as mock_provider
from app.payments.usdt.networks import get_network
from app.services.fulfillment import visible_deliveries
from app.services.orders import claim_order_for_user, normalize_email, order_by_token, order_for_user
from app.services.payments import PaymentError, available_channels, handle_webhook, start_payment
from app.services.support import TicketError, create_ticket
from app.web.deps import Viewer, csrf_form, form_str, get_store, get_viewer, rate_limit
from app.web.templating import redirect, render, set_cookie

router = APIRouter()
RECENT_COOKIE = "recent_orders"


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="recent-orders")


def _recent(request: Request) -> dict[str, str]:
    raw = request.cookies.get(RECENT_COOKIE)
    if not raw or len(raw) > 4000:
        return {}
    try:
        data = _signer().loads(raw)
    except BadSignature:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def remember_order(request: Request, response, order_no: str, token: str) -> None:  # type: ignore[no-untyped-def]
    data = _recent(request)
    data.pop(order_no, None)
    data[order_no] = token
    data = dict(list(data.items())[-5:])
    set_cookie(response, RECENT_COOKIE, _signer().dumps(data), max_age=60 * 60 * 24 * 30)


def access_order(
    request: Request, db: Session, store: Store, order_no: str, viewer: Viewer
) -> tuple[Order | None, str | None]:
    """Returns (order, token-if-known). Never discloses anything based on the order number alone."""
    token = request.query_params.get("t") or _recent(request).get(order_no)
    key = request.query_params.get("k")
    order = order_by_token(db, store.id, order_no, token, key)
    if order is None:
        order = order_for_user(db, store.id, order_no, viewer.user)
        token = None
    elif not (token and order_by_token(db, store.id, order_no, token)):
        token = None
    return order, token


def _qr_data_uri(text: str) -> str:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def _order_context(db: Session, order: Order) -> dict[str, object]:
    attempts = (
        db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.order_id == order.id)
            .order_by(PaymentAttempt.id.desc())
        )
        .scalars()
        .all()
    )
    active = next(
        (a for a in attempts if a.status in ACTIVE_ATTEMPT_STATES and a.expires_at > utcnow()), None
    )
    usdt = None
    if active is not None and active.provider == "usdt":
        network = get_network(active.extra["network"])
        transfers = (
            db.execute(
                select(ChainTransfer).where(ChainTransfer.attempt_id == active.id).order_by(ChainTransfer.id)
            )
            .scalars()
            .all()
        )
        usdt = {
            "network": network,
            "address": active.extra["address"],
            "qr": _qr_data_uri(active.extra["address"]),
            "amount": active.pay_amount,
            "received": active.received_amount,
            "remaining": max(active.pay_amount - active.received_amount, 0),
            "transfers": [
                {
                    "tx": t.tx_hash,
                    "url": network.tx_url(t.tx_hash),
                    "amount": t.amount,
                    "status": t.status,
                    "confirmations": t.confirmations,
                }
                for t in transfers
            ],
        }
    events = (
        db.execute(
            select(OrderEvent)
            .where(OrderEvent.order_id == order.id, OrderEvent.visible_to_customer.is_(True))
            .order_by(OrderEvent.id)
        )
        .scalars()
        .all()
    )
    show_deliveries = order.paid_at is not None
    channels = available_channels(db, order) if order.payment_status == "unpaid" else []
    return {
        "order": order,
        "item": order.items[0] if order.items else None,
        "attempts": attempts,
        "active_attempt": active,
        "usdt": usdt,
        "events": events,
        "usable_channels": [c for c, reason in channels if reason is None],
        "deliveries": visible_deliveries(db, order) if show_deliveries else [],
    }


@router.get("/o/{order_no}")
def order_page(
    request: Request,
    order_no: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "order_view", 120, 60)
    order, token = access_order(request, db, store, order_no, viewer)
    if order is None:
        raise HTTPException(404, "Order not found")
    response = render(request, "shop/order.html", **_order_context(db, order), returned=False, error=None)
    if token:
        remember_order(request, response, order.order_no, token)
    return response


@router.get("/o/{order_no}/return")
def order_return(
    request: Request,
    order_no: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    """Landing page after the hosted checkout. Shows status only; it never marks anything paid."""
    order, _ = access_order(request, db, store, order_no, viewer)
    if order is None:
        return render(request, "shop/lookup.html", sent=False, returned=True)
    return render(request, "shop/order.html", **_order_context(db, order), returned=True, error=None)


@router.post("/o/{order_no}/pay")
def order_pay(
    request: Request,
    order_no: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "pay", 15, 600)
    order, _ = access_order(request, db, store, order_no, viewer)
    if order is None:
        raise HTTPException(404, "Order not found")
    try:
        attempt = start_payment(db, order, form_str(form, "channel", 40))
    except PaymentError as exc:
        db.rollback()
        db.refresh(order)
        return render(
            request,
            "shop/order.html",
            status_code=400,
            **_order_context(db, order),
            returned=False,
            error=exc.code,
        )
    if attempt.checkout_url and attempt.checkout_url.startswith("https://"):
        return RedirectResponse(attempt.checkout_url, status_code=303)
    if attempt.checkout_url:
        return redirect(attempt.checkout_url)
    return redirect(f"/o/{order.order_no}")


@router.get("/api/orders/{order_no}/status")
def order_status(
    request: Request,
    order_no: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "status_poll", 60, 60)
    order, _ = access_order(request, db, store, order_no, viewer)
    if order is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {
            "payment_status": order.payment_status,
            "fulfillment_status": order.fulfillment_status,
            "lifecycle": order.lifecycle,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/o/{order_no}/claim")
def order_claim(
    request: Request,
    order_no: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    if viewer.user is None:
        return redirect(f"/account/login?next=/o/{order_no}")
    order, token = access_order(request, db, store, order_no, viewer)
    if order is None or token is None and order.user_id != viewer.user.id:
        raise HTTPException(404, "Order not found")
    claim_order_for_user(db, order, viewer.user)
    db.commit()
    return redirect(f"/o/{order.order_no}")


@router.post("/o/{order_no}/ticket")
def order_ticket(
    request: Request,
    order_no: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "ticket", 10, 3600)
    order, _ = access_order(request, db, store, order_no, viewer)
    if order is None:
        raise HTTPException(404, "Order not found")
    try:
        ticket, token = create_ticket(
            db,
            store_id=store.id,
            email=order.email,
            order=order,
            subject=form_str(form, "subject", 160) or f"Order {order.order_no}",
            body=form_str(form, "body", 5000),
            user_id=viewer.user.id if viewer.user else None,
        )
    except TicketError:
        db.rollback()
        return redirect(f"/o/{order.order_no}#support")
    db.commit()
    return redirect(f"/support/t/{ticket.ticket_no}?t={token}")


@router.get("/lookup")
def lookup_form(request: Request, store: Store = Depends(get_store), viewer: Viewer = Depends(get_viewer)):
    return render(request, "shop/lookup.html", sent=False, returned=False)


@router.post("/lookup")
def lookup_submit(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    """Sends the access link to the order's e-mail. Same response whether or not it matched."""
    rate_limit(request, "lookup", 5, 900)
    if not verify_turnstile(form_str(form, "cf-turnstile-response", 2048), request.state.client_ip):
        return render(
            request, "shop/lookup.html", status_code=400, sent=False, returned=False, error="turnstile"
        )
    order_no = form_str(form, "order_no", 32).upper()
    try:
        email = normalize_email(form_str(form, "email", 254))
    except ValueError:
        email = ""
    order = db.execute(
        select(Order).where(Order.order_no == order_no, Order.store_id == store.id)
    ).scalar_one_or_none()
    if order is not None and email and secrets.compare_digest(order.email, email):
        rate_limit(request, "lookup_order", 3, 3600, extra=order_no)
        enqueue(db, "send_email", {"template": "order_lookup", "order_id": order.id}, max_attempts=3)
        db.commit()
    return render(request, "shop/lookup.html", sent=True, returned=False)


# Development-only simulated checkout ------------------------------------------------------------


@router.get("/pay/mock/{attempt_no}")
def mock_checkout(
    request: Request,
    attempt_no: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    if not get_settings().mock_allowed:
        raise HTTPException(404, "Not found")
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.attempt_no == attempt_no[:40])
    ).scalar_one_or_none()
    if attempt is None or attempt.provider != "mock":
        raise HTTPException(404, "Not found")
    order = db.get(Order, attempt.order_id)
    return render(request, "shop/mock_pay.html", attempt=attempt, order=order)


@router.post("/pay/mock/{attempt_no}")
def mock_checkout_submit(
    request: Request,
    attempt_no: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    if not get_settings().mock_allowed:
        raise HTTPException(404, "Not found")
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.attempt_no == attempt_no[:40])
    ).scalar_one_or_none()
    if attempt is None or attempt.provider != "mock":
        raise HTTPException(404, "Not found")
    outcome = "payment.succeeded" if form_str(form, "outcome", 20) == "success" else "payment.failed"
    body = mock_provider.build_event(
        attempt.attempt_no,
        outcome,
        str(attempt.pay_amount),
        attempt.pay_currency,
        f"evt_{secrets.token_hex(8)}",
    )
    # Same verification path as a real provider webhook.
    handle_webhook(
        db,
        attempt.channel_code,
        {mock_provider.SIG_HEADER: mock_provider.sign(body)},
        body,
        request.state.client_ip,
    )
    order = db.get(Order, attempt.order_id)
    assert order is not None
    return redirect(f"/o/{order.order_no}/return")
