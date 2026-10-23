from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.ops import AuditLog
from app.models.orders import OrderEvent


def audit(
    db: Session,
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_ip: str = "",
    target_type: str = "",
    target_id: object = "",
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            created_at=utcnow(),
            actor_user_id=actor_user_id,
            actor_ip=actor_ip,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details or {},
        )
    )


def order_event(
    db: Session,
    order_id: int,
    kind: str,
    message: str = "",
    *,
    data: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    visible: bool = False,
) -> None:
    db.add(
        OrderEvent(
            order_id=order_id,
            created_at=utcnow(),
            kind=kind,
            message=message[:2000],
            data=data or {},
            actor_user_id=actor_user_id,
            visible_to_customer=visible,
        )
    )
