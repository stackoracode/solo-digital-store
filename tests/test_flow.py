"""End-to-end vertical slice over HTTP: list product -> browse -> guest checkout -> simulated payment ->
automatic delivery -> order status -> admin handles an exception. Plus queue and i18n checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select

from app.jobs.queue import enqueue, mark_failed
from app.models.ops import JOB_DEAD, Job, ReviewItem
from app.models.orders import Order
from tests.conftest import drain, make_product


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def test_guest_checkout_to_automatic_delivery(client, db, store):
    product = make_product(db, store, codes=2, title="Pro License")
    home = client.get("/")
    assert home.status_code == 200 and "Pro License" in home.text
    page = client.get(f"/p/{product.slug}")
    assert page.status_code == 200 and "Instant delivery" in page.text
    resp = client.post(
        f"/p/{product.slug}/checkout",
        data={
            "csrf_token": _csrf(page.text),
            "sku_id": product.skus[0].id,
            "quantity": "1",
            "email": "guest@example.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    order_url = resp.headers["location"]
    order_page = client.get(order_url)
    assert order_page.status_code == 200 and "Test payment" in order_page.text
    order_no = order_url.split("/o/")[1].split("?")[0]
    pay = client.post(
        f"/o/{order_no}/pay",
        data={"csrf_token": _csrf(order_page.text), "channel": "mock"},
        follow_redirects=False,
    )
    assert pay.status_code == 303 and pay.headers["location"].startswith("/pay/mock/")
    mock_page = client.get(pay.headers["location"])
    done = client.post(
        pay.headers["location"],
        data={"csrf_token": _csrf(mock_page.text), "outcome": "success"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    drain()
    # The cookie set at checkout lets the same browser reopen the order without the token in the URL.
    final = client.get(f"/o/{order_no}")
    assert final.status_code == 200
    assert "CODE-" in final.text
    status = client.get(f"/api/orders/{order_no}/status").json()
    assert status == {"payment_status": "paid", "fulfillment_status": "delivered", "lifecycle": "completed"}


def test_chinese_locale_renders(client, db, store):
    make_product(db, store, codes=1)
    resp = client.get("/?lang=zh-CN")
    assert "自动发货" in resp.text and 'lang="zh-CN"' in resp.text


def test_out_of_stock_after_payment_lands_in_review_and_admin_can_deliver(db, store):
    from app.services.fulfillment import deliver_manual
    from tests.conftest import make_order, mock_webhook, start

    product = make_product(db, store, codes=1)
    order, _ = make_order(db, store, product, quantity=1)
    # Stock disappears between checkout and payment.
    from app.models.inventory import CardCode

    for code in db.execute(select(CardCode)).scalars():
        code.status = "void"
    db.commit()
    mock_webhook(db, start(db, order))
    drain()
    db.expire_all()
    assert db.get(Order, order.id).fulfillment_status == "awaiting_stock"
    assert db.execute(select(ReviewItem).where(ReviewItem.kind == "out_of_stock")).scalar_one()
    deliver_manual(db, order.id, order.items[0].id, "REPLACEMENT-CODE", user_id=None)
    db.commit()
    db.expire_all()
    assert db.get(Order, order.id).fulfillment_status == "delivered"


def test_job_dedupe_and_dead_letter(db, store):
    enqueue(db, "send_email", {"template": "nope"}, dedupe_key="k1", max_attempts=1)
    enqueue(db, "send_email", {"template": "nope"}, dedupe_key="k1", max_attempts=1)
    db.commit()
    jobs = db.execute(select(Job)).scalars().all()
    assert len(jobs) == 1
    jobs[0].attempts = 1
    db.commit()
    assert mark_failed(db, jobs[0].id, "boom") == JOB_DEAD
    assert db.execute(select(ReviewItem).where(ReviewItem.kind == "job_dead")).scalar_one()


def test_unknown_job_kind_does_not_crash_worker(db, store):
    enqueue(db, "does_not_exist", {}, max_attempts=1)
    db.commit()
    drain()
    job = db.execute(select(Job)).scalar_one()
    assert job.status == JOB_DEAD


def test_locale_catalogs_have_identical_keys():
    base = Path(__file__).resolve().parent.parent / "app" / "locales"
    en = json.loads((base / "en-US.json").read_text(encoding="utf-8"))
    zh = json.loads((base / "zh-CN.json").read_text(encoding="utf-8"))
    assert set(en) == set(zh)
    assert all(v.strip() for v in zh.values())


def test_template_static_keys_exist():
    base = Path(__file__).resolve().parent.parent / "app"
    en = json.loads((base / "locales" / "en-US.json").read_text(encoding="utf-8"))
    missing = set()
    for tpl in (base / "web" / "templates").rglob("*.html"):
        for key in re.findall(r"\bt\('([a-z0-9_.]+)'\)", tpl.read_text(encoding="utf-8")):
            if key not in en:
                missing.add(f"{tpl.name}:{key}")
        for key in re.findall(r"\bt\('([a-z0-9_.]+[a-z0-9_])',", tpl.read_text(encoding="utf-8")):
            if key not in en:
                missing.add(f"{tpl.name}:{key}")
    assert not missing, missing
