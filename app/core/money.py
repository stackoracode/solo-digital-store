"""Decimal-only money helpers. Floats are rejected on purpose."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

CURRENCY_EXPONENT = {"USD": 2, "EUR": 2, "CNY": 2, "HKD": 2, "JPY": 0, "USDT": 6}
CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "CNY": "¥", "HKD": "HK$", "JPY": "¥"}


class MoneyError(ValueError):
    pass


def to_decimal(value: object) -> Decimal:
    if isinstance(value, float):
        raise MoneyError("float values are not allowed for money")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"invalid amount: {value!r}") from exc
    if not d.is_finite():
        raise MoneyError("amount must be finite")
    return d


def quantum(currency: str) -> Decimal:
    exp = CURRENCY_EXPONENT.get(currency.upper(), 2)
    return Decimal(1).scaleb(-exp)


def q(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(quantum(currency), rounding=ROUND_HALF_UP)


def q_up(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(quantum(currency), rounding=ROUND_UP)


def parse_price(raw: str, currency: str = "USD") -> Decimal:
    d = to_decimal(raw)
    if d <= 0:
        raise MoneyError("price must be positive")
    if d != q(d, currency):
        raise MoneyError("too many decimal places")
    if d > Decimal("1000000"):
        raise MoneyError("price too large")
    return d


def display(amount: Decimal, currency: str) -> str:
    """Display format string as used by Waffo (e.g. "29.00")."""
    return format(q(amount, currency), "f")


def fmt(amount: Decimal, currency: str) -> str:
    sym = CURRENCY_SYMBOL.get(currency.upper())
    value = format(q(amount, currency), ",f")
    return f"{sym}{value}" if sym else f"{value} {currency}"


def fee(amount: Decimal, percent: Decimal, fixed: Decimal, currency: str) -> Decimal:
    return q(amount * percent + fixed, currency)
