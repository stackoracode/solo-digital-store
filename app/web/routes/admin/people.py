"""Admin: support tickets, agents & commissions, customer list."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models.ops import Ticket
from app.models.stores import Agent, Commission, Store
from app.models.users import User
from app.services.agents import AgentError, set_agent_status
from app.services.audit import audit
from app.services.support import TicketError, add_message, messages
from app.web.deps import csrf_form, form_str, get_store, require_admin, require_owner
from app.web.templating import redirect, render

router = APIRouter()


@router.get("/tickets")
def tickets(
    request: Request, status: str = "open", store: Store = Depends(get_store), db: Session = Depends(get_db)
):
    stmt = select(Ticket).where(Ticket.store_id == store.id).order_by(Ticket.updated_at.desc()).limit(200)
    if status in ("open", "answered", "closed"):
        stmt = stmt.where(Ticket.status == status)
    return render(request, "admin/tickets.html", tickets=db.execute(stmt).scalars().all(), status=status)


def _ticket(db: Session, store: Store, ticket_id: int) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.store_id != store.id:
        raise HTTPException(404, "Ticket not found")
    return ticket


@router.get("/tickets/{ticket_id}")
def ticket_detail(
    request: Request, ticket_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)
):
    ticket = _ticket(db, store, ticket_id)
    return render(request, "admin/ticket_detail.html", ticket=ticket, messages=messages(db, ticket))


@router.post("/tickets/{ticket_id}")
def ticket_reply(
    request: Request,
    ticket_id: int,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ticket = _ticket(db, store, ticket_id)
    if form_str(form, "action", 10) == "close":
        ticket.status = "closed"
    else:
        try:
            add_message(db, ticket, form_str(form, "body", 5000), author_type="admin", user_id=user.id)
        except TicketError:
            db.rollback()
            return redirect(f"/admin/tickets/{ticket.id}")
    db.commit()
    return redirect(f"/admin/tickets/{ticket.id}")


@router.get("/agents")
def agents(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Agent, User).join(User, User.id == Agent.user_id).order_by(Agent.id.desc())
    ).all()
    commissions = db.execute(select(Commission).order_by(Commission.id.desc()).limit(100)).scalars().all()
    return render(request, "admin/agents.html", agents=rows, commissions=commissions)


@router.post("/agents/{agent_id}")
def agent_update(
    request: Request,
    agent_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    try:
        rate_raw = form_str(form, "rate_percent", 10)
        rate = (Decimal(rate_raw) / 100) if rate_raw else None
        set_agent_status(db, agent, form_str(form, "status", 16), rate, form_str(form, "note", 2000))
    except (AgentError, InvalidOperation):
        db.rollback()
        return redirect("/admin/agents")
    audit(
        db,
        "agent_updated",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="agent",
        target_id=agent.id,
        details={"status": agent.status, "rate": str(agent.commission_rate)},
    )
    db.commit()
    return redirect("/admin/agents")


@router.post("/commissions/{commission_id}")
def commission_update(
    request: Request,
    commission_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    commission = db.get(Commission, commission_id, with_for_update=True)
    if commission is None:
        raise HTTPException(404, "Not found")
    target = form_str(form, "status", 16)
    allowed = {"pending": ("approved",), "approved": ("paid",)}
    if target in allowed.get(commission.status, ()):
        commission.status = target
        audit(
            db,
            "commission_status",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="commission",
            target_id=commission.id,
            details={"status": target},
        )
        db.commit()
    return redirect("/admin/agents")


@router.get("/customers")
def customers(request: Request, q: str = "", db: Session = Depends(get_db)):
    stmt = select(User).order_by(User.id.desc()).limit(200)
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q.strip()[:100].replace('%', '')}%"))
    return render(request, "admin/customers.html", users=db.execute(stmt).scalars().all(), q=q)
