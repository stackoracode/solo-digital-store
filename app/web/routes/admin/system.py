"""Admin: store settings, categories, AI provider registry (config only), custom domains (future agents)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_settings
from app.core.middleware import invalidate_domain_cache
from app.core.netsafe import UnsafeURLError, validate_outbound_url
from app.core.security import encrypt, new_token
from app.db import get_db
from app.models.catalog import Category
from app.models.ops import AIProvider
from app.models.stores import Store, StoreDomain
from app.models.users import User
from app.services.audit import audit
from app.services.catalog import slugify
from app.services.settings_store import DEFAULTS, get_settings_map, set_setting
from app.web.deps import clear_store_cache, csrf_form, form_str, get_store, require_owner
from app.web.templating import redirect, render

router = APIRouter()
AI_KINDS = ("openai", "gemini", "anthropic", "compatible")
AI_TASKS = ("after_sales", "security_log_analysis", "copywriting", "order_triage")
HOST_RE_MAX = 253


@router.get("/settings")
def settings_page(
    request: Request,
    store: Store = Depends(get_store),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    categories = (
        db.execute(select(Category).where(Category.store_id == store.id).order_by(Category.sort))
        .scalars()
        .all()
    )
    providers = db.execute(select(AIProvider).order_by(AIProvider.id)).scalars().all()
    domains = db.execute(select(StoreDomain).order_by(StoreDomain.id)).scalars().all()
    return render(
        request,
        "admin/settings.html",
        values=get_settings_map(db, store.id),
        keys=list(DEFAULTS),
        categories=categories,
        providers=providers,
        ai_kinds=AI_KINDS,
        ai_tasks=AI_TASKS,
        domains=domains,
        allowlist=get_settings().ai_endpoint_allowlist,
        error=request.query_params.get("error"),
    )


@router.post("/settings")
def settings_save(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    for key, default in DEFAULTS.items():
        if isinstance(default, bool):
            set_setting(db, store.id, key, form.get(key) == "1")
        elif key in form:
            set_setting(db, store.id, key, form_str(form, key, 50000))
    audit(db, "settings_updated", actor_user_id=user.id, actor_ip=request.state.client_ip)
    db.commit()
    return redirect("/admin/settings")


@router.post("/categories")
def category_save(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    name = form_str(form, "name", 80)
    if not name:
        return redirect("/admin/settings?error=category_name")
    cat_id = form_str(form, "category_id", 20)
    cat = db.get(Category, int(cat_id)) if cat_id.isdigit() else None
    if cat is None:
        cat = Category(
            store_id=store.id, slug=slugify(form_str(form, "slug", 64) or name), name=name, translations={}
        )
        db.add(cat)
    cat.name = name
    zh = form_str(form, "name_zh", 80)
    cat.translations = {"zh-CN": {"name": zh}} if zh else {}
    cat.sort = int(form_str(form, "sort", 6)) if form_str(form, "sort", 6).isdigit() else 0
    cat.is_active = form.get("is_active", "1") == "1"
    db.commit()
    return redirect("/admin/settings#categories")


@router.post("/ai-providers")
def ai_provider_save(
    request: Request,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    kind = form_str(form, "kind", 24)
    if kind not in AI_KINDS:
        return redirect("/admin/settings?error=ai_kind")
    try:
        base_url = validate_outbound_url(
            form_str(form, "base_url", 300), get_settings().ai_endpoint_allowlist
        )
    except UnsafeURLError as exc:
        return redirect(f"/admin/settings?error=ai_url_{str(exc)[:80]}")
    name = form_str(form, "name", 60)
    model_id = form_str(form, "model_id", 120)
    if not name or not model_id:
        return redirect("/admin/settings?error=ai_required")
    provider = db.execute(select(AIProvider).where(AIProvider.name == name)).scalar_one_or_none()
    if provider is None:
        provider = AIProvider(name=name, kind=kind, base_url=base_url, model_id=model_id, tasks=[])
        db.add(provider)
    provider.kind = kind
    provider.base_url = base_url
    # Model IDs must be checked against the provider's current official model list by the operator.
    provider.model_id = model_id
    api_key = form_str(form, "api_key", 500)
    if api_key:  # write-only: never rendered back
        provider.api_key_enc = encrypt(api_key)
        provider.api_key_last4 = api_key[-4:]
    provider.timeout_seconds = max(1, min(120, int(form_str(form, "timeout_seconds", 4) or 30)))
    provider.max_tokens = max(1, min(32000, int(form_str(form, "max_tokens", 6) or 1024)))
    provider.tasks = [t for t in form.getlist("tasks") if t in AI_TASKS]
    provider.enabled = form.get("enabled") == "1"
    audit(
        db,
        "ai_provider_saved",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="ai_provider",
        target_id=name,
        details={"base_url": base_url, "model_id": model_id},
    )
    db.commit()
    return redirect("/admin/settings#ai")


@router.post("/domains")
def domain_add(
    request: Request,
    form: FormData = Depends(csrf_form),
    store: Store = Depends(get_store),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """Phase 1: records a pending domain + verification token. Routing needs status=verified, which is
    set only after DNS TXT / Cloudflare for SaaS custom hostname validation (not automated yet)."""
    host = form_str(form, "hostname", HOST_RE_MAX).lower().rstrip(".")
    if not host or "/" in host or ":" in host or "." not in host or host in get_settings().primary_hosts:
        return redirect("/admin/settings?error=domain_invalid")
    if db.scalar(select(StoreDomain.id).where(StoreDomain.hostname == host)):
        return redirect("/admin/settings?error=domain_exists")
    db.add(StoreDomain(store_id=store.id, hostname=host, status="pending", verification_token=new_token(16)))
    audit(
        db,
        "domain_added",
        actor_user_id=user.id,
        actor_ip=request.state.client_ip,
        target_type="domain",
        target_id=host,
    )
    db.commit()
    return redirect("/admin/settings#domains")


@router.post("/domains/{domain_id}/status")
def domain_status(
    request: Request,
    domain_id: int,
    form: FormData = Depends(csrf_form),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    domain = db.get(StoreDomain, domain_id)
    if domain is None:
        raise HTTPException(404, "Domain not found")
    status = form_str(form, "status", 16)
    if status == "disabled":
        domain.status = "disabled"
        audit(
            db,
            "domain_disabled",
            actor_user_id=user.id,
            actor_ip=request.state.client_ip,
            target_type="domain",
            target_id=domain.hostname,
        )
        db.commit()
        clear_store_cache()
        invalidate_domain_cache()
    return redirect("/admin/settings#domains")
