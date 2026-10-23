"""Object-level authorization, CSRF, host spoofing, cross-store isolation, SSRF, form safety, money types."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.core.money import MoneyError, to_decimal
from app.core.netsafe import UnsafeURLError, validate_outbound_url
from app.models.stores import Store, StoreDomain
from app.services.forms import FormDefinitionError, normalize_fields, validate_submission
from tests.conftest import FormStub, make_order, make_product


def _csrf(client, path="/"):
    html = client.get(path).text
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert match, "csrf token not rendered"
    return match.group(1)


def test_order_page_requires_token(client, db, store):
    product = make_product(db, store, codes=1)
    order, token = make_order(db, store, product)
    assert client.get(f"/o/{order.order_no}").status_code == 404  # enumerable number alone is useless
    assert client.get(f"/o/{order.order_no}?t=wrong").status_code == 404
    assert client.get(f"/api/orders/{order.order_no}/status").status_code == 404
    assert client.get(f"/o/{order.order_no}?t={token}").status_code == 200


def test_other_users_order_not_accessible(client, db, store):
    from app.models.users import User

    owner = User(email="owner@example.com", role="customer")
    other = User(email="other@example.com", role="customer")
    db.add_all([owner, other])
    db.commit()
    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product, user=owner)
    from app.services.auth import create_session

    sid = create_session(db, other, ip="1.1.1.1", user_agent="t")
    db.commit()
    client.cookies.set("sid", sid)
    assert client.get(f"/o/{order.order_no}").status_code == 404


def test_signed_email_link_grants_access_and_rotation_revokes(client, db, store):
    from app.services.orders import order_link, rotate_access_token

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product)
    link = order_link(order).replace("http://testserver", "")
    assert client.get(link).status_code == 200
    rotate_access_token(order)
    db.commit()
    fresh = client.__class__(client.app, base_url="http://testserver")
    assert fresh.get(link).status_code == 404


def test_post_without_csrf_is_rejected(client, db, store):
    product = make_product(db, store, codes=1)
    resp = client.post(f"/p/{product.slug}/checkout", data={"sku_id": product.skus[0].id, "email": "a@b.co"})
    assert resp.status_code == 403


def test_cross_origin_post_is_rejected(client, db, store):
    product = make_product(db, store, codes=1)
    token = _csrf(client, f"/p/{product.slug}")
    resp = client.post(
        f"/p/{product.slug}/checkout",
        headers={"origin": "https://evil.example"},
        data={"csrf_token": token, "sku_id": product.skus[0].id, "email": "a@b.co"},
    )
    assert resp.status_code == 403


def test_unknown_or_unverified_host_is_rejected(client, db, store):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    db.add(
        StoreDomain(store_id=store.id, hostname="pending.example", status="pending", verification_token="x")
    )
    db.commit()
    assert client.get("/", headers={"host": "pending.example"}).status_code == 421


def test_verified_agent_domain_cannot_read_main_store_orders(db, store):
    from fastapi.testclient import TestClient

    from app.main import app

    agent_store = Store(code="agent1", name="Agent store", is_default=False, status="active")
    db.add(agent_store)
    db.flush()
    db.add(
        StoreDomain(
            store_id=agent_store.id, hostname="agent.example", status="verified", verification_token="x"
        )
    )
    db.commit()
    product = make_product(db, store, codes=1)
    order, token = make_order(db, store, product)
    with TestClient(app, base_url="http://agent.example") as c:
        assert c.get("/").status_code == 200
        # Valid token, but the order belongs to another store: not found.
        assert c.get(f"/o/{order.order_no}?t={token}").status_code == 404


def test_spoofed_forwarded_for_is_ignored_without_trusted_proxy():
    from app.config import get_settings
    from app.core.middleware import client_ip_from

    scope = {
        "type": "http",
        "client": ("198.51.100.7", 1234),
        "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"cf-connecting-ip", b"5.6.7.8")],
    }
    assert client_ip_from(scope, get_settings(), []) == "198.51.100.7"


def test_trusted_proxy_header_is_used():
    import ipaddress

    from app.config import get_settings
    from app.core.middleware import client_ip_from

    scope = {
        "type": "http",
        "client": ("10.0.0.2", 1234),
        "headers": [(b"x-forwarded-for", b"9.9.9.9, 10.0.0.1")],
    }
    assert client_ip_from(scope, get_settings(), [ipaddress.ip_network("10.0.0.0/8")]) == "9.9.9.9"


def test_security_headers(client):
    resp = client.get("/")
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_body_size_limit(client):
    resp = client.post(
        "/lookup", content=b"x" * 2_000_000, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert resp.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://169.254.169.254/latest",
        "https://127.0.0.1/",
        "https://user:pw@api.openai.com/",
        "https://evil.example/v1",
        "https://api.openai.com:8443/",
    ],
)
def test_ssrf_guard_rejects(url):
    with pytest.raises(UnsafeURLError):
        validate_outbound_url(url, ["api.openai.com"], resolve=False)


def test_ssrf_guard_accepts_allowlisted():
    assert validate_outbound_url("https://api.openai.com/v1", ["api.openai.com"], resolve=False)


@pytest.mark.parametrize("label", ["Account password", "游戏密码", "Card CVV", "Recovery seed phrase", "PIN"])
def test_forms_reject_sensitive_fields(label):
    with pytest.raises(FormDefinitionError):
        normalize_fields([{"key": "x", "label": label, "type": "text"}])


def test_form_validation_is_server_side():
    fields = normalize_fields(
        [
            {
                "key": "role_id",
                "label": "Role ID",
                "type": "text",
                "required": True,
                "max_length": 10,
                "pattern": r"\d+",
            },
            {
                "key": "server",
                "label": "Server",
                "type": "select",
                "options": ["Asia", "EU"],
                "required": True,
            },
            {"key": "extras", "label": "Extras", "type": "checkbox", "options": ["A", "B"]},
            {"key": "level", "label": "Level", "type": "number", "min": "1", "max": "99"},
        ]
    )
    clean, errors = validate_submission(
        fields,
        FormStub(
            {"f_role_id": "12345", "f_server": "EU", "f_extras": ["A"], "f_level": "50", "f_injected": "x"}
        ),
    )
    assert errors == {} and "injected" not in clean
    _, errors = validate_submission(
        fields, FormStub({"f_role_id": "abc", "f_server": "Mars", "f_extras": ["Z"], "f_level": "100"})
    )
    assert errors == {
        "role_id": "pattern",
        "server": "invalid_option",
        "extras": "invalid_option",
        "level": "too_large",
    }
    _, errors = validate_submission(fields, FormStub({}))
    assert errors == {"role_id": "required", "server": "required"}


def test_form_snapshot_survives_later_edits(db, store):
    from app.services.forms import save_form_version

    product = make_product(db, store, codes=2)
    save_form_version(db, product, [{"key": "uid", "label": "UID", "type": "text", "required": True}])
    db.commit()
    order, _ = make_order(db, store, product, form={"f_uid": "42"})
    save_form_version(db, product, [{"key": "uid", "label": "Player UID", "type": "text", "required": True}])
    db.commit()
    item = order.items[0]
    assert item.form_data == {"uid": "42"}
    assert item.snapshot["form_version"] == 1
    assert item.snapshot["form_fields"][0]["label"] == "UID"
    assert item.snapshot["unit_price"] == "10.000000" or Decimal(item.snapshot["unit_price"]) == Decimal("10")


def test_money_rejects_float():
    with pytest.raises(MoneyError):
        to_decimal(0.1)
    assert to_decimal("0.10") == Decimal("0.10")


def test_production_config_guards():
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(env="production", _env_file=None)


def test_admin_area_requires_staff_session(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303 and "/account/staff-login" in resp.headers["location"]


def test_admin_requires_mfa_setup(client, db, store):
    from app.cli import create_admin
    from app.services.auth import create_session

    admin = create_admin(db, "boss@example.com", "correct horse battery staple")
    sid = create_session(db, admin, ip="1.1.1.1", user_agent="t")
    db.commit()
    client.cookies.set("sid", sid)
    resp = client.get("/admin/orders", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/mfa/setup"
