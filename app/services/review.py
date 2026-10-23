"""Manual review queue: the safety net for anything that must not be auto-processed."""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.ops import ReviewItem


def open_review(
    db: Session,
    *,
    kind: str,
    summary: str,
    dedupe_key: str | None = None,
    severity: str = "high",
    order_id: int | None = None,
    attempt_id: int | None = None,
    procurement_id: int | None = None,
    transfer_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    stmt = pg_insert(ReviewItem).values(
        kind=kind,
        severity=severity,
        status="open",
        dedupe_key=dedupe_key,
        order_id=order_id,
        attempt_id=attempt_id,
        procurement_id=procurement_id,
        transfer_id=transfer_id,
        summary=summary[:2000],
        data=data or {},
        resolution="",
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    db.execute(stmt)


def resolve_review(
    db: Session, item: ReviewItem, user_id: int, resolution: str, status: str = "resolved"
) -> None:
    item.status = status
    item.resolution = resolution[:2000]
    item.resolved_by = user_id
    item.resolved_at = utcnow()
