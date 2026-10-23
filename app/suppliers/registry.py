from __future__ import annotations

from app.config import get_settings
from app.suppliers.base import SupplierAdapter
from app.suppliers.mock import MockSupplier

_SUPPLIERS: dict[str, SupplierAdapter] = {}
MOCK_SUPPLIER = MockSupplier()


def get_supplier(code: str) -> SupplierAdapter:
    if code == "mock":
        if get_settings().is_production:
            raise KeyError("mock supplier is disabled in production")
        return MOCK_SUPPLIER
    try:
        return _SUPPLIERS[code]
    except KeyError as exc:
        raise KeyError(f"no adapter registered for supplier {code}") from exc


def register_supplier(adapter: SupplierAdapter) -> None:
    _SUPPLIERS[adapter.code] = adapter
