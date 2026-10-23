"""Watch-only deposit address derivation from an account-level xpub (m/44'/60'/0').

Only the extended *public* key is configured on the server. Private keys / mnemonics never touch the
application or database; recovering or sweeping funds is an offline, separately controlled process.
"""

from __future__ import annotations

from functools import lru_cache

from bip_utils import Bip44, Bip44Changes, Bip44Coins


class XpubError(ValueError):
    pass


@lru_cache(maxsize=4)
def _account(xpub: str):  # type: ignore[no-untyped-def]
    if not xpub:
        raise XpubError("USDT_XPUB is not configured")
    if xpub.startswith(("xprv", "tprv")):
        raise XpubError("refusing a private extended key; configure the account xpub only")
    ctx = Bip44.FromExtendedKey(xpub, Bip44Coins.ETHEREUM)
    if not ctx.IsPublicOnly():
        raise XpubError("extended key must be public-only")
    return ctx


def derive_address(xpub: str, index: int) -> str:
    if index < 0 or index >= 2**31:
        raise XpubError("index out of range")
    ctx = _account(xpub).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
    return ctx.PublicKey().ToAddress().lower()
