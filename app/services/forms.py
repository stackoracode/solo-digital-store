"""Custom order form definitions and strict server-side validation of submitted values."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product, ProductFormVersion

FIELD_TYPES = ("text", "number", "radio", "checkbox", "select", "textarea")
MAX_FIELDS = 20
MAX_OPTIONS = 30
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Standard fields must never collect credentials or payment data.
FORBIDDEN_TERMS = (
    "password",
    "passwd",
    "pwd",
    "passcode",
    "pin",
    "cvv",
    "cvc",
    "card number",
    "cardnumber",
    "credit card",
    "otp",
    "2fa",
    "seed",
    "mnemonic",
    "private key",
    "privatekey",
    "secret",
    "security code",
    "ssn",
    "密码",
    "口令",
    "验证码",
    "助记词",
    "私钥",
    "信用卡",
    "银行卡",
    "支付密码",
    "安全码",
)


class FormDefinitionError(ValueError):
    pass


def _contains_forbidden(text: str) -> str | None:
    low = text.lower()
    for term in FORBIDDEN_TERMS:
        if term in ("pin", "otp", "pwd", "2fa", "ssn"):
            if re.search(rf"(^|[^a-z]){re.escape(term)}([^a-z]|$)", low):
                return term
        elif term in low:
            return term
    return None


def normalize_fields(raw_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate an admin-provided field list and return a canonical, sorted copy."""
    if len(raw_fields) > MAX_FIELDS:
        raise FormDefinitionError(f"at most {MAX_FIELDS} fields")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, f in enumerate(raw_fields):
        key = str(f.get("key", "")).strip().lower()
        label = str(f.get("label", "")).strip()[:80]
        ftype = str(f.get("type", "text")).strip()
        if not key and not label:
            continue
        if not KEY_RE.match(key):
            raise FormDefinitionError(f"invalid field key: {key!r} (a-z, 0-9, _; start with a letter)")
        if key in seen:
            raise FormDefinitionError(f"duplicate field key: {key}")
        if ftype not in FIELD_TYPES:
            raise FormDefinitionError(f"unsupported field type: {ftype}")
        if not label:
            raise FormDefinitionError(f"field {key} needs a label")
        help_text = str(f.get("help", "")).strip()[:200]
        for text in (key.replace("_", " "), label, help_text):
            term = _contains_forbidden(text)
            if term:
                raise FormDefinitionError(
                    f"field {key}: collecting '{term}' is not allowed in standard order fields"
                )
        options = [str(o).strip()[:80] for o in (f.get("options") or []) if str(o).strip()]
        if ftype in ("radio", "checkbox", "select"):
            if not options:
                raise FormDefinitionError(f"field {key} needs options")
            if len(options) > MAX_OPTIONS or len(set(options)) != len(options):
                raise FormDefinitionError(f"field {key}: options must be unique and at most {MAX_OPTIONS}")
        else:
            options = []
        max_length = int(f.get("max_length") or (2000 if ftype == "textarea" else 200))
        max_length = max(1, min(max_length, 4000 if ftype == "textarea" else 500))
        pattern = str(f.get("pattern") or "").strip()
        if pattern:
            if len(pattern) > 200:
                raise FormDefinitionError(f"field {key}: pattern too long")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise FormDefinitionError(f"field {key}: invalid pattern ({exc})") from exc
        field: dict[str, Any] = {
            "key": key,
            "label": label,
            "type": ftype,
            "help": help_text,
            "required": bool(f.get("required")),
            "max_length": max_length,
            "pattern": pattern,
            "options": options,
            "order": int(f.get("order", idx) or 0),
        }
        if ftype == "number":
            for bound in ("min", "max"):
                val = f.get(bound)
                if val not in (None, ""):
                    try:
                        field[bound] = str(Decimal(str(val)))
                    except InvalidOperation as exc:
                        raise FormDefinitionError(f"field {key}: invalid {bound}") from exc
        seen.add(key)
        out.append(field)
    out.sort(key=lambda x: (x["order"], x["key"]))
    return out


def save_form_version(
    db: Session, product: Product, raw_fields: list[dict[str, Any]]
) -> ProductFormVersion | None:
    """Create a new immutable version when the definition changed. Returns the current version."""
    fields = normalize_fields(raw_fields)
    current = (
        db.get(ProductFormVersion, product.current_form_version_id)
        if product.current_form_version_id
        else None
    )
    if current is not None and current.fields == fields:
        return current
    if current is None and not fields:
        return None
    next_version = (
        db.scalar(
            select(func.max(ProductFormVersion.version)).where(ProductFormVersion.product_id == product.id)
        )
        or 0
    ) + 1
    version = ProductFormVersion(product_id=product.id, version=next_version, fields=fields)
    db.add(version)
    db.flush()
    product.current_form_version_id = version.id
    return version


def validate_submission(fields: list[dict[str, Any]], form: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate submitted values (from a multi-dict with .getlist) against field definitions.

    Unknown keys are ignored (never stored). Returns (clean_values, errors_by_key).
    """
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in fields:
        key, ftype = f["key"], f["type"]
        name = f"f_{key}"
        values = (
            [str(v) for v in form.getlist(name)] if hasattr(form, "getlist") else [str(form.get(name, ""))]
        )
        values = [v.strip() for v in values if v is not None]
        if ftype == "checkbox":
            chosen = [v for v in values if v]
            if any(v not in f["options"] for v in chosen) or len(set(chosen)) != len(chosen):
                errors[key] = "invalid_option"
                continue
            if f["required"] and not chosen:
                errors[key] = "required"
                continue
            if chosen:
                clean[key] = chosen
            continue
        value = values[0] if values else ""
        if len(values) > 1:
            errors[key] = "invalid"
            continue
        if not value:
            if f["required"]:
                errors[key] = "required"
            continue
        if len(value) > f["max_length"]:
            errors[key] = "too_long"
            continue
        if "\x00" in value:
            errors[key] = "invalid"
            continue
        if ftype in ("radio", "select"):
            if value not in f["options"]:
                errors[key] = "invalid_option"
                continue
        elif ftype == "number":
            try:
                num = Decimal(value)
                if not num.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                errors[key] = "not_a_number"
                continue
            if "min" in f and num < Decimal(f["min"]):
                errors[key] = "too_small"
                continue
            if "max" in f and num > Decimal(f["max"]):
                errors[key] = "too_large"
                continue
            value = str(num)
        elif ftype == "text" and ("\n" in value or "\r" in value):
            errors[key] = "invalid"
            continue
        if f.get("pattern") and not re.fullmatch(f["pattern"], value):
            errors[key] = "pattern"
            continue
        clean[key] = value
    return clean, errors
