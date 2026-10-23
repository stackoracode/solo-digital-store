"""In-memory simulated supplier for development and tests (no network, no real money)."""

from __future__ import annotations

import secrets
import threading
from decimal import Decimal

from app.suppliers.base import Quote, SupplierOrder, SupplierRejected, SupplierTimeout


class MockSupplier:
    code = "mock"
    idempotent_submit = True

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.orders: dict[str, SupplierOrder] = {}
        self.unit_cost = Decimal("1.00")
        self.in_stock = True
        # Test knobs
        self.timeout_after_success = False  # order is created upstream but the response is lost
        self.reject_next = False
        self.pending_queries = 0  # number of queries that report "pending" before success
        self.place_calls = 0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def quote(self, supplier_sku: str, quantity: int) -> Quote:
        return Quote(unit_cost=self.unit_cost, currency="USD", available=self.in_stock)

    def place_order(self, supplier_sku, quantity, idempotency_key, max_unit_cost):  # type: ignore[no-untyped-def]
        with self.lock:
            self.place_calls += 1
            if idempotency_key in self.orders:
                return self.orders[idempotency_key]
            if self.reject_next or not self.in_stock:
                self.reject_next = False
                raise SupplierRejected("out of stock")
            if max_unit_cost is not None and self.unit_cost > max_unit_cost:
                raise SupplierRejected("price above cap")
            order = SupplierOrder(
                status="succeeded",
                supplier_order_no=f"MOCK-{secrets.token_hex(4).upper()}",
                contents=[f"MOCK-CODE-{secrets.token_hex(6).upper()}" for _ in range(quantity)],
                cost=self.unit_cost * quantity,
            )
            self.orders[idempotency_key] = order
            if self.timeout_after_success:
                self.timeout_after_success = False
                raise SupplierTimeout("simulated timeout after upstream success")
            return order

    def query_order(self, idempotency_key: str) -> SupplierOrder:
        with self.lock:
            if self.pending_queries > 0:
                self.pending_queries -= 1
                return SupplierOrder(status="pending")
            return self.orders.get(idempotency_key) or SupplierOrder(status="not_found")
