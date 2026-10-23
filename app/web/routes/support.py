"""Pre-sale contact and ticket threads (guest access by secret token or signed e-mail link)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.core.turnstile import verify_turnstile
from app.db import get_db
from app.models.ops import Ticket
from app.models.stores import Store
from app.services.orders import OrderError, normalize_email
from app.services.support import TicketError, add_message, create_ticket, messages, ticket_by_token
from app.web.deps import Viewer, csrf_form, form_str, get_store, get_viewer, rate_limit
from app.web.templating import redirect, render

router = APIRouter(prefix="/support")


@router.get("")
def contact(request: Request, store: Store = Depends(get_store), viewer: Viewer = Depends(get_viewer)):
    return render(request, "support/contact.html", error=None)


@router.post("")
def contact_submit(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "presale", 5, 3600)
    if not verify_turnstile(form_str(form, "cf-turnstile-response", 2048), request.state.client_ip):
        return render(request, "support/contact.html", status_code=400, error="turnstile")
    try:
        email = normalize_email(form_str(form, "email", 254))
        ticket, token = create_ticket(
            db,
            store_id=store.id,
            email=email,
            subject=form_str(form, "subject", 160),
            body=form_str(form, "body", 5000),
            category="pre_sale",
            user_id=viewer.user.id if viewer.user else None,
        )
    except (OrderError, TicketError):
        db.rollback()
        return render(request, "support/contact.html", status_code=400, error="invalid")
    db.commit()
    return redirect(f"/support/t/{ticket.ticket_no}?t={token}")


def _ticket(request: Request, db: Session, store: Store, ticket_no: str, viewer: Viewer) -> Ticket:
    ticket = ticket_by_token(
        db, store.id, ticket_no[:32], request.query_params.get("t"), request.query_params.get("k")
    )
    if ticket is None and viewer.user is not None:
        from sqlalchemy import select

        ticket = db.execute(
            select(Ticket).where(
                Ticket.ticket_no == ticket_no[:32],
                Ticket.store_id == store.id,
                Ticket.user_id == viewer.user.id,
            )
        ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    return ticket


@router.get("/t/{ticket_no}")
def ticket_view(
    request: Request,
    ticket_no: str,
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    ticket = _ticket(request, db, store, ticket_no, viewer)
    return render(
        request,
        "support/ticket.html",
        ticket=ticket,
        messages=messages(db, ticket),
        t=request.query_params.get("t", ""),
        k=request.query_params.get("k", ""),
    )


@router.post("/t/{ticket_no}")
def ticket_reply(
    request: Request,
    ticket_no: str,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    viewer: Viewer = Depends(get_viewer),
    db: Session = Depends(get_db),
):
    rate_limit(request, "ticket_reply", 20, 3600)
    ticket = _ticket(request, db, store, ticket_no, viewer)
    try:
        add_message(
            db,
            ticket,
            form_str(form, "body", 5000),
            author_type="customer",
            user_id=viewer.user.id if viewer.user else None,
        )
    except TicketError:
        db.rollback()
    db.commit()
    query = request.url.query
    return redirect(f"/support/t/{ticket.ticket_no}" + (f"?{query}" if query else ""))
