"""Support tickets (pre-sale and order-linked after-sale) with message history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import constant_eq, new_token, token_hash
from app.db import utcnow
from app.jobs.queue import enqueue
from app.models.ops import Ticket, TicketMessage
from app.models.orders import AS_OPEN, Order
from app.services.orders import check_access_key, make_access_key, new_public_no


class TicketError(ValueError):
    pass


def _clean(text: str, limit: int) -> str:
    text = (text or "").replace("\x00", "").strip()
    if not text:
        raise TicketError("empty")
    return text[:limit]


def create_ticket(
    db: Session,
    *,
    store_id: int,
    email: str,
    subject: str,
    body: str,
    category: str = "pre_sale",
    order: Order | None = None,
    user_id: int | None = None,
) -> tuple[Ticket, str]:
    token = new_token(18)
    ticket = Ticket(
        ticket_no=new_public_no("TK"),
        store_id=store_id,
        order_id=order.id if order else None,
        user_id=user_id,
        email=email,
        category="after_sale" if order else category,
        subject=_clean(subject, 160),
        status="open",
        access_token_hash=token_hash(token),
    )
    db.add(ticket)
    db.flush()
    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            created_at=utcnow(),
            author_type="customer",
            author_user_id=user_id,
            body=_clean(body, 5000),
        )
    )
    if order is not None:
        order.after_sale_status = AS_OPEN
    enqueue(
        db,
        "send_email",
        {"template": "ticket_created", "ticket_id": ticket.id},
        dedupe_key=f"email:ticket_created:{ticket.id}",
    )
    return ticket, token


def ticket_by_token(
    db: Session, store_id: int, ticket_no: str, token: str | None, key: str | None = None
) -> Ticket | None:
    if not token and not key:
        return None
    ticket = db.execute(
        select(Ticket).where(Ticket.ticket_no == ticket_no, Ticket.store_id == store_id)
    ).scalar_one_or_none()
    if ticket is None:
        return None
    if token and constant_eq(ticket.access_token_hash, token_hash(token)):
        return ticket
    if key and check_access_key("ticket", ticket.ticket_no, ticket.access_token_hash, key):
        return ticket
    return None


def ticket_link(ticket: Ticket) -> str:
    base = get_settings().public_base_url.rstrip("/")
    key = make_access_key("ticket", ticket.ticket_no, ticket.access_token_hash)
    return f"{base}/support/t/{ticket.ticket_no}?k={key}"


def add_message(
    db: Session, ticket: Ticket, body: str, *, author_type: str, user_id: int | None = None
) -> None:
    if ticket.status == "closed" and author_type == "customer":
        ticket.status = "open"
    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            created_at=utcnow(),
            author_type=author_type,
            author_user_id=user_id,
            body=_clean(body, 5000),
        )
    )
    if author_type == "admin":
        ticket.status = "answered"
        enqueue(
            db,
            "send_email",
            {"template": "ticket_replied", "ticket_id": ticket.id},
            dedupe_key=f"email:ticket_reply:{ticket.id}:{utcnow().timestamp():.0f}",
        )
    else:
        ticket.status = "open"


def messages(db: Session, ticket: Ticket) -> list[TicketMessage]:
    return list(
        db.execute(
            select(TicketMessage).where(TicketMessage.ticket_id == ticket.id).order_by(TicketMessage.id)
        ).scalars()
    )
