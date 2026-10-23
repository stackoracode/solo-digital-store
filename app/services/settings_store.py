"""Per-store business settings editable by the admin (contacts, policies, legal texts)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ops import Setting

DEFAULTS: dict[str, Any] = {
    "store_name": "Solo Digital Store",
    "tagline": "Digital goods, delivered fast.",
    "support_email": "support@example.com",
    "support_contacts": "",  # free text lines, e.g. "Telegram: @handle"
    "support_hours": "Mon-Fri 09:00-18:00 UTC",
    "default_refund_policy": (
        "Unused digital items can be refunded within 7 days if the code was not revealed or redeemed. "
        "Delivered and revealed codes are non-refundable unless they are defective."
    ),
    "terms_text": "",
    "privacy_text": "",
    "announcement": "",
    "email_codes_in_mail": False,
}

EDITABLE_KEYS = tuple(DEFAULTS)


def get_settings_map(db: Session, store_id: int) -> dict[str, Any]:
    rows = db.execute(select(Setting).where(Setting.store_id == store_id)).scalars().all()
    data = dict(DEFAULTS)
    data.update({r.key: r.value for r in rows if r.key in DEFAULTS})
    return data


def set_setting(db: Session, store_id: int, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.execute(
        select(Setting).where(Setting.store_id == store_id, Setting.key == key)
    ).scalar_one_or_none()
    if row is None:
        db.add(Setting(store_id=store_id, key=key, value=value))
    else:
        row.value = value
