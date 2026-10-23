"""Operator CLI: `python -m app.cli <command>`.

bootstrap            create default store + payment channel rows (idempotent)
create-admin EMAIL   create/upgrade an owner account (password read from STDIN or prompt)
seed-demo            development-only demo catalog with card codes (refuses in production)
"""

from __future__ import annotations

import getpass
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import hash_password
from app.db import SessionLocal
from app.models.catalog import Category, Product
from app.models.payments import PaymentChannel
from app.models.stores import Store
from app.models.users import User
from app.services.catalog import quick_create_product
from app.services.forms import save_form_version
from app.services.inventory import import_codes

DEFAULT_CHANNELS = [
    # code, provider, display name, config, sort, fee %, fixed fee
    ("mock", "mock", "Test payment (simulated)", {}, 0, Decimal("0"), Decimal("0")),
    (
        "waffo",
        "waffo",
        "Card / Apple Pay / Google Pay (Waffo)",
        {"tax_category": "digital_goods"},
        10,
        Decimal("0.039"),
        Decimal("0.50"),
    ),
    ("usdt_xlayer", "usdt", "USDT0 on X Layer", {"network": "xlayer"}, 20, Decimal("0"), Decimal("0")),
    ("usdt_polygon", "usdt", "USDT0 on Polygon", {"network": "polygon"}, 30, Decimal("0"), Decimal("0")),
]


def bootstrap(db: Session) -> Store:
    store = db.execute(select(Store).where(Store.is_default.is_(True))).scalar_one_or_none()
    if store is None:
        store = Store(code="main", name=get_settings().app_name, is_default=True, status="active")
        db.add(store)
        db.flush()
    for code, provider, name, config, sort, pct, fixed in DEFAULT_CHANNELS:
        if db.execute(select(PaymentChannel).where(PaymentChannel.code == code)).scalar_one_or_none() is None:
            db.add(
                PaymentChannel(
                    code=code,
                    provider=provider,
                    display_name=name,
                    config=config,
                    sort=sort,
                    # New channels start disabled in test mode; only "mock" is on outside production.
                    enabled=provider == "mock" and get_settings().mock_allowed,
                    mode="test",
                    fee_percent=pct,
                    fee_fixed=fixed,
                )
            )
    db.commit()
    return store


def create_admin(db: Session, email: str, password: str, role: str = "owner") -> User:
    if len(password) < 12:
        raise SystemExit("password must be at least 12 characters")
    email = email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=email.split("@")[0])
        db.add(user)
    user.password_hash = hash_password(password)
    user.role = role
    user.is_active = True
    db.commit()
    return user


def seed_demo(db: Session) -> None:
    if get_settings().is_production:
        raise SystemExit("seed-demo is disabled in production")
    store = bootstrap(db)
    if db.execute(select(Product).where(Product.store_id == store.id)).first():
        print("catalog not empty; skipping")
        return
    cat = Category(
        store_id=store.id,
        slug="software",
        name="Software keys",
        translations={"zh-CN": {"name": "软件激活码"}},
    )
    db.add(cat)
    db.flush()
    p1 = quick_create_product(
        db,
        store_id=store.id,
        title="Demo Software License (1 year)",
        price="9.90",
        description="Demo product for the development environment.\nDelivered instantly.",
        fulfillment_method="auto_card",
        category_id=cat.id,
    )
    p1.translations = {
        "zh-CN": {"title": "演示软件授权（1 年）", "description": "开发环境演示商品，付款后自动发货。"}
    }
    p1.usage_guide = "Open the app → Settings → License → paste the key."
    import_codes(
        db, p1.skus[0], "\n".join(f"DEMO-KEY-{i:04d}-ABCD" for i in range(1, 21)), label="demo batch"
    )
    p2 = quick_create_product(
        db,
        store_id=store.id,
        title="Custom Design Template Setup",
        price="25.00",
        description="We prepare a template with your brand name. Manual delivery within 2 hours.",
        fulfillment_method="manual",
    )
    p2.delivery_eta_minutes = 120
    save_form_version(
        db,
        p2,
        [
            {"key": "brand_name", "label": "Brand name", "type": "text", "required": True, "max_length": 60},
            {
                "key": "style",
                "label": "Style",
                "type": "select",
                "options": ["Minimal", "Bold", "Playful"],
                "required": True,
            },
            {"key": "notes", "label": "Notes", "type": "textarea", "max_length": 500},
        ],
    )
    p3 = quick_create_product(
        db,
        store_id=store.id,
        title="Sourced Digital Item (mock supplier)",
        price="4.99",
        description="Bought from the simulated supplier after payment.",
        fulfillment_method="supplier",
    )
    p3.supplier_code = "mock"
    p3.skus[0].supplier_sku = "MOCK-ITEM"
    p3.skus[0].max_unit_cost = Decimal("2.00")
    db.commit()
    print("demo catalog created")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]
    with SessionLocal() as db:
        if cmd == "bootstrap":
            bootstrap(db)
            print("bootstrap complete")
        elif cmd == "create-admin" and len(argv) >= 2:
            bootstrap(db)
            password = (
                sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Password: ")
            )
            user = create_admin(db, argv[1], password)
            print(f"owner account ready: {user.email} (enable TOTP at first sign-in)")
        elif cmd == "seed-demo":
            seed_demo(db)
        else:
            print(__doc__)
            raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
