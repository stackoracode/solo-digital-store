from __future__ import annotations

from sqlalchemy import JSON, Numeric
from sqlalchemy.dialects.postgresql import JSONB

# Money is always Decimal, never float. 6 fractional digits covers USDT (6 decimals) and fiat.
MONEY = Numeric(20, 6, asdecimal=True)
# Raw on-chain integer token units (uint256 fits in 78 digits).
TOKEN_UNITS = Numeric(78, 0, asdecimal=True)

JSONType = JSON().with_variant(JSONB(), "postgresql")
