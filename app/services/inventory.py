"""Card-code inventory: encrypted import with de-duplication, concurrency-safe claiming, reports."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt, keyed_hash
from app.db import utcnow
from app.models.catalog import Product, Sku
from app.models.inventory import CardBatch, CardCode

MAX_IMPORT_LINES = 10_000
MAX_CODE_LENGTH = 1000


@dataclass
class ImportResult:
    batch_id: int
    imported: int
    duplicates: int
    rejected: int


def fingerprint(code: str) -> str:
    return keyed_hash(code.strip(), "card")


def parse_codes(text: str) -> list[str]:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    return [line for line in lines if line]


def import_codes(
    db: Session, sku: Sku, raw_text: str, *, label: str = "", user_id: int | None = None
) -> ImportResult:
    codes = parse_codes(raw_text)
    if len(codes) > MAX_IMPORT_LINES:
        raise ValueError(f"at most {MAX_IMPORT_LINES} codes per import")
    batch = CardBatch(
        sku_id=sku.id, label=label[:120] or f"Import {utcnow():%Y-%m-%d %H:%M}", created_by=user_id
    )
    db.add(batch)
    db.flush()
    imported = duplicates = rejected = 0
    seen: set[str] = set()
    for code in codes:
        if len(code) > MAX_CODE_LENGTH or "\x00" in code:
            rejected += 1
            continue
        fp = fingerprint(code)
        if fp in seen:
            duplicates += 1
            continue
        seen.add(fp)
        inserted_id = db.execute(
            pg_insert(CardCode)
            .values(
                sku_id=sku.id, batch_id=batch.id, secret_enc=encrypt(code), fingerprint=fp, status="available"
            )
            .on_conflict_do_nothing(index_elements=["sku_id", "fingerprint"])
            .returning(CardCode.id)
        ).scalar_one_or_none()
        if inserted_id is not None:
            imported += 1
        else:
            duplicates += 1
    batch.imported_count = imported
    batch.duplicate_count = duplicates
    return ImportResult(batch.id, imported, duplicates, rejected)


def claim_codes(db: Session, sku_id: int, quantity: int, order_item_id: int) -> list[CardCode]:
    """Lock `quantity` available codes. SKIP LOCKED lets concurrent orders take *different* codes.

    Must run inside the caller's transaction; all-or-nothing (returns [] if not enough stock).
    """
    rows = list(
        db.execute(
            select(CardCode)
            .where(CardCode.sku_id == sku_id, CardCode.status == "available")
            .order_by(CardCode.id)
            .limit(quantity)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    if len(rows) < quantity:
        return []
    now = utcnow()
    for code in rows:
        code.status = "sold"
        code.order_item_id = order_item_id
        code.sold_at = now
    return rows


def void_code(db: Session, code: CardCode) -> bool:
    if code.status != "available":
        return False
    code.status = "void"
    return True


def stock_report(db: Session, store_id: int) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            Product.id,
            Product.title,
            Product.low_stock_threshold,
            Sku.id,
            Sku.name,
            CardCode.status,
            func.count(CardCode.id),
        )
        .join(Sku, Sku.product_id == Product.id)
        .outerjoin(CardCode, CardCode.sku_id == Sku.id)
        .where(Product.store_id == store_id, Product.fulfillment_method == "auto_card")
        .group_by(Product.id, Product.title, Product.low_stock_threshold, Sku.id, Sku.name, CardCode.status)
    ).all()
    report: dict[int, dict[str, object]] = {}
    for pid, title, threshold, sku_id, sku_name, status, count in rows:
        entry = report.setdefault(
            sku_id,
            {
                "product_id": pid,
                "product": title,
                "sku_id": sku_id,
                "sku": sku_name,
                "threshold": threshold,
                "available": 0,
                "sold": 0,
                "void": 0,
            },
        )
        if status:
            entry[status] = int(count)
    for entry in report.values():
        entry["low"] = int(entry["available"]) <= int(entry["threshold"])  # type: ignore[call-overload]
    return sorted(report.values(), key=lambda e: (not e["low"], str(e["product"])))


def export_codes_csv(db: Session, sku_id: int, status: str = "available") -> str:
    """Export decrypted codes. Callers must enforce owner role + MFA and write an audit log."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "batch_id", "status", "code", "sold_at", "order_item_id"])
    rows = db.execute(
        select(CardCode).where(CardCode.sku_id == sku_id, CardCode.status == status).order_by(CardCode.id)
    ).scalars()
    for row in rows:
        code = decrypt(row.secret_enc)
        # Neutralise spreadsheet formula injection.
        if code[:1] in ("=", "+", "-", "@", "\t", "\r"):
            code = "'" + code
        writer.writerow([row.id, row.batch_id, row.status, code, row.sold_at or "", row.order_item_id or ""])
    return buf.getvalue()
