"""EVM network/token parameters loaded from config/usdt_networks.json (never inferred from token names)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


@dataclass(frozen=True)
class Network:
    key: str
    display_name: str
    chain_id: int
    gas_token: str
    rpc_urls: tuple[str, ...]
    token_symbol: str
    token_contract: str
    token_decimals: int
    confirmations: int
    reorg_depth: int
    max_block_range: int
    start_block: int
    explorer_tx_url: str = ""
    explorer_address_url: str = ""
    extra: dict = field(default_factory=dict)

    def tx_url(self, tx_hash: str) -> str:
        return self.explorer_tx_url.format(tx=tx_hash) if self.explorer_tx_url else ""

    def address_url(self, address: str) -> str:
        return self.explorer_address_url.format(address=address) if self.explorer_address_url else ""


def _parse(key: str, raw: dict) -> Network:
    contract = str(raw["token_contract"]).lower()
    if not (contract.startswith("0x") and len(contract) == 42):
        raise ValueError(f"{key}: invalid token contract")
    return Network(
        key=key,
        display_name=raw["display_name"],
        chain_id=int(raw["chain_id"]),
        gas_token=raw.get("gas_token", ""),
        rpc_urls=tuple(raw["rpc_urls"]),
        token_symbol=raw["token_symbol"],
        token_contract=contract,
        token_decimals=int(raw["token_decimals"]),
        confirmations=max(1, int(raw["confirmations"])),
        reorg_depth=max(1, int(raw.get("reorg_depth", raw["confirmations"]))),
        max_block_range=max(10, int(raw.get("max_block_range", 500))),
        start_block=int(raw.get("start_block", 0)),
        explorer_tx_url=raw.get("explorer_tx_url", ""),
        explorer_address_url=raw.get("explorer_address_url", ""),
    )


@lru_cache
def load_networks(path: str | None = None) -> dict[str, Network]:
    file = Path(path or get_settings().usdt_networks_file)
    data = json.loads(file.read_text(encoding="utf-8"))
    return {key: _parse(key, raw) for key, raw in data["networks"].items()}


def get_network(key: str) -> Network:
    nets = load_networks()
    if key not in nets:
        raise KeyError(f"unknown network {key}")
    return nets[key]
