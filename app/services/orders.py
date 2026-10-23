"""Order creation (with immutable snapshots) and object-level access control."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from email_validator import EmailNotValidError, validate_email
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.money import q
from app.core.security import constant_eq, new_token, token_hash
from app.db import utcnow
from app.models.catalog import Product, ProductFormVersion, Sku
from app.models.orders import LC_EXPIRED, LC_PENDING_PAYMENT, PAY_UNPAID, Order, OrderItem
from app.models.stores import Agent
from app.models.users import User
from app.services.audit import order_event
from app.services.catalog import stock_by_sku
from app.services.forms import validate_submission
from app.services.settings_store import get_settings_map

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class OrderError(ValueError):
    def __init__(self, code: str, field_errors: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field_errors = field_errors or {}


def new_public_no(prefix: str) -> str:
    """Human-friendly reference. Not a credential: access always needs the token or an owner session."""
    rand = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"{prefix}{utcnow():%y%m%d}{rand}"


def normalize_email(raw: str) -> str:
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise OrderError("invalid_email") from exc


def resolve_agent(db: Session, code: str | None, buyer: User | None, email: str) -> Agent | None:
    if not code:
        return None
    agent = db.execute(
        select(Agent).where(Agent.code == code[:24], Agent.status == "active")
    ).scalar_one_or_none()
    if agent is None:
        return None
    agent_user = db.get(User, agent.user_id)
    # No self-referral commissions.
    if buyer is not None and buyer.id == agent.user_id:
        return None
    if agent_user is not None and agent_user.email and agent_user.email == email:
        return None
    return agent


def create_order(
    db: Session,
    *,
    store_id: int,
    product: Product,
    sku: Sku,
    quantity: int,
    email: str,
    form: Any,
    locale: str,
    client_ip: str,
    user: User | None = None,
    agent_code: str | None = None,
) -> tuple[Order, str]:
    """Create an unpaid order. Returns (order, clear access token). Caller commits."""
    if product.store_id != store_id or product.status != "active":
        raise OrderError("product_unavailable")
    if sku.product_id != product.id or not sku.is_active:
        raise OrderError("sku_unavailable")
    if not product.min_qty <= quantity <= product.max_qty:
        raise OrderError("quantity")
    email = normalize_email(email)

    fields: list[dict[str, Any]] = []
    form_version: ProductFormVersion | None = None
    if product.current_form_version_id:
        form_version = db.get(ProductFormVersion, product.current_form_version_id)
        fields = form_version.fields if form_version else []
    form_values, field_errors = validate_submission(fields, form)
    if field_errors:
        raise OrderError("form_invalid", field_errors)

    if product.fulfillment_method == "auto_card":
        available = stock_by_sku(db, [sku.id]).get(sku.id, 0)
        if available < quantity:
            raise OrderError("out_of_stock")

    settings = get_settings()
    store_settings = get_settings_map(db, store_id)
    line_total = q(sku.price * quantity, sku.currency)
    token = new_token(24)
    order = Order(
        order_no=new_public_no("SO"),
        store_id=store_id,
        user_id=user.id if user else None,
        agent_id=None,
        email=email,
        locale=locale,
        access_token_hash=token_hash(token),
        currency=sku.currency,
        subtotal=line_total,
        total=line_total,
        lifecycle=LC_PENDING_PAYMENT,
        payment_status=PAY_UNPAID,
        expires_at=utcnow() + timedelta(minutes=settings.order_unpaid_ttl_minutes),
        client_ip=client_ip,
    )
    agent = resolve_agent(db, agent_code, user, email)
    if agent is not None:
        order.agent_id = agent.id
    db.add(order)
    db.flush()
    snapshot = {
        "product_title": product.tr("title", locale),
        "product_slug": product.slug,
        "sku_name": sku.name,
        "unit_price": str(sku.price),
        "currency": sku.currency,
        "fulfillment_method": product.fulfillment_method,
        "delivery_eta_minutes": product.delivery_eta_minutes,
        "refund_policy": product.refund_policy or store_settings["default_refund_policy"],
        "form_version": form_version.version if form_version else None,
        "form_fields": fields,
        "supplier_code": product.supplier_code,
        "supplier_sku": sku.supplier_sku,
        "max_unit_cost": str(sku.max_unit_cost) if sku.max_unit_cost is not None else None,
    }
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            sku_id=sku.id,
            quantity=quantity,
            unit_price=sku.price,
            line_total=line_total,
            fulfillment_method=product.fulfillment_method,
            form_version_id=form_version.id if form_version else None,
            form_data=form_values,
            snapshot=snapshot,
        )
    )
    order_event(db, order.id, "created", f"Order created for {line_total} {sku.currency}", visible=True)
    db.flush()
    db.refresh(order)
    return order, token


_LINK_SALT = "order-access-link"
LINK_MAX_AGE = 60 * 60 * 24 * 60  # e-mailed links stay valid for 60 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=_LINK_SALT)


def make_access_key(kind: str, ref: str, token_hash_value: str) -> str:
    """Signed, expiring access key for e-mails. Bound to the current token hash: rotating revokes it."""
    return _serializer().dumps({"k": kind, "r": ref, "h": token_hash_value[:20]})


def check_access_key(kind: str, ref: str, token_hash_value: str, key: str | None) -> bool:
    if not key or len(key) > 300:
        return False
    try:
        data = _serializer().loads(key, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return (
        isinstance(data, dict)
        and data.get("k") == kind
        and data.get("r") == ref
        and constant_eq(str(data.get("h", "")), token_hash_value[:20])
    )


def order_by_token(
    db: Session, store_id: int, order_no: str, token: str | None, key: str | None = None
) -> Order | None:
    """Object-level access: requires the secret token or a signed e-mail key. Scoped to the store."""
    if (not token and not key) or len(order_no) > 32:
        return None
    order = db.execute(
        select(Order).where(Order.order_no == order_no, Order.store_id == store_id)
    ).scalar_one_or_none()
    if order is None:
        return None
    if token and constant_eq(order.access_token_hash, token_hash(token)):
        return order
    if key and check_access_key("order", order.order_no, order.access_token_hash, key):
        return order
    return None


def order_link(order: Order) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/o/{order.order_no}?k={make_access_key('order', order.order_no, order.access_token_hash)}"


def order_for_user(db: Session, store_id: int, order_no: str, user: User | None) -> Order | None:
    if user is None:
        return None
    return db.execute(
        select(Order).where(Order.order_no == order_no, Order.store_id == store_id, Order.user_id == user.id)
    ).scalar_one_or_none()


def rotate_access_token(order: Order) -> str:
    token = new_token(24)
    order.access_token_hash = token_hash(token)
    return token


def expire_stale_orders(db: Session, limit: int = 500) -> int:
    rows = (
        db.execute(
            select(Order)
            .where(
                Order.lifecycle == LC_PENDING_PAYMENT,
                Order.payment_status == PAY_UNPAID,
                Order.expires_at < utcnow(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for order in rows:
        order.lifecycle = LC_EXPIRED
        order_event(db, order.id, "expired", "Unpaid order expired", visible=True)
    db.commit()
    return len(rows)


def claim_order_for_user(db: Session, order: Order, user: User) -> bool:
    if order.user_id is not None:
        return order.user_id == user.id
    order.user_id = user.id
    order_event(db, order.id, "claimed", f"Linked to account #{user.id}", actor_user_id=user.id)
    return True
