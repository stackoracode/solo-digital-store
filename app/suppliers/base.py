"""Supplier Adapter contract. Only officially authorised supplier APIs may be implemented here.

Never scrape, bypass logins or automate a supplier's consumer UI (see docs/architecture.md, ADR-005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


class SupplierTimeout(Exception):
    """Outcome unknown: the order may or may not exist upstream. Must be queried, not re-sent."""


class SupplierRejected(Exception):
    """Definitive refusal (no charge happened), e.g. out of stock or invalid SKU."""


class SupplierUnavailable(Exception):
    """Request was not accepted (connection refused before sending); safe to retry later."""


@dataclass
class Quote:
    unit_cost: Decimal
    currency: str
    available: bool = True


@dataclass
class SupplierOrder:
    # pending | succeeded | failed | not_found
    status: str
    supplier_order_no: str | None = None
    contents: list[str] = field(default_factory=list)
    cost: Decimal | None = None
    message: str = ""


class SupplierAdapter(Protocol):
    code: str
    # True only if the supplier dedupes on our idempotency key (re-sending is then harmless).
    idempotent_submit: bool

    def quote(self, supplier_sku: str, quantity: int) -> Quote: ...

    def place_order(
        self, supplier_sku: str, quantity: int, idempotency_key: str, max_unit_cost: Decimal | None
    ) -> SupplierOrder: ...

    def query_order(self, idempotency_key: str) -> SupplierOrder: ...
