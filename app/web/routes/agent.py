"""Agent (affiliate) self-service: apply, referral link, statistics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.db import get_db
from app.models.stores import Agent, Commission, Store
from app.models.users import User
from app.services.agents import AgentError, agent_stats, apply_agent
from app.web.deps import csrf_form, form_str, get_store, rate_limit, require_user
from app.web.templating import redirect, render

router = APIRouter(prefix="/agent")


@router.get("")
def agent_home(
    request: Request,
    store: Store = Depends(get_store),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    agent = db.execute(select(Agent).where(Agent.user_id == user.id)).scalar_one_or_none()
    stats = agent_stats(db, agent) if agent and agent.status == "active" else None
    commissions = []
    if agent is not None:
        commissions = (
            db.execute(
                select(Commission)
                .where(Commission.agent_id == agent.id)
                .order_by(Commission.id.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
    link = f"{get_settings().public_base_url.rstrip('/')}/?ref={agent.code}" if agent else ""
    return render(request, "agent/home.html", agent=agent, stats=stats, commissions=commissions, link=link)


@router.post("/apply")
def agent_apply(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rate_limit(request, "agent_apply", 3, 3600)
    try:
        apply_agent(db, user, form_str(form, "note", 2000))
        db.commit()
    except AgentError:
        db.rollback()
    return redirect("/agent")
