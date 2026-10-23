from __future__ import annotations

from app.payments.base import PaymentProvider
from app.payments.mock import MockProvider
from app.payments.usdt.provider import UsdtProvider
from app.payments.waffo import WaffoProvider

_PROVIDERS: dict[str, PaymentProvider] = {
    "mock": MockProvider(),
    "waffo": WaffoProvider(),
    "usdt": UsdtProvider(),
}


def get_provider(name: str) -> PaymentProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown payment provider {name}") from exc


def register_provider(name: str, provider: PaymentProvider) -> None:
    """Test hook / extension point for new adapters."""
    _PROVIDERS[name] = provider
