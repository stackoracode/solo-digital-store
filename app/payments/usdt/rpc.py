"""Minimal read-only EVM JSON-RPC client with endpoint failover. No signing, no transfers."""

from __future__ import annotations

import itertools
import logging
import os
from typing import Any, Protocol

import httpx

from app.payments.usdt.networks import Network

log = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DECIMALS_SELECTOR = "0x313ce567"
BALANCE_OF_SELECTOR = "0x70a08231"


class RpcError(Exception):
    pass


class EvmReader(Protocol):
    def chain_id(self) -> int: ...
    def block_number(self) -> int: ...
    def get_logs(
        self, from_block: int, to_block: int, token: str, to_addresses: list[str]
    ) -> list[dict[str, Any]]: ...
    def get_receipt(self, tx_hash: str) -> dict[str, Any] | None: ...
    def token_decimals(self, token: str) -> int: ...
    def balance_of(self, token: str, address: str) -> int: ...


def address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


class JsonRpcReader:
    def __init__(self, network: Network, timeout: float = 10.0) -> None:
        override = os.environ.get(f"USDT_RPC_{network.key.upper()}", "")
        urls = [u.strip() for u in override.split(",") if u.strip()] or list(network.rpc_urls)
        if not urls:
            raise RpcError(f"no RPC URLs for {network.key}")
        self.urls = urls
        self.timeout = timeout
        self._ids = itertools.count(1)

    def _call(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        for url in self.urls:
            try:
                resp = httpx.post(
                    url,
                    json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RpcError(f"{method}: {data['error']}")
                return data.get("result")
            except (httpx.HTTPError, ValueError, RpcError) as exc:
                last_error = exc
                log.warning("rpc call failed", extra={"path": url.split("?")[0]})
        raise RpcError(f"all RPC endpoints failed for {method}: {last_error}")

    def chain_id(self) -> int:
        return int(self._call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self._call("eth_blockNumber", []), 16)

    def get_logs(
        self, from_block: int, to_block: int, token: str, to_addresses: list[str]
    ) -> list[dict[str, Any]]:
        result = self._call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": token,
                    "topics": [TRANSFER_TOPIC, None, [address_topic(a) for a in to_addresses]],
                }
            ],
        )
        return list(result or [])

    def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self._call("eth_getTransactionReceipt", [tx_hash])

    def token_decimals(self, token: str) -> int:
        return int(self._call("eth_call", [{"to": token, "data": DECIMALS_SELECTOR}, "latest"]), 16)

    def balance_of(self, token: str, address: str) -> int:
        data = BALANCE_OF_SELECTOR + address.lower().removeprefix("0x").rjust(64, "0")
        return int(self._call("eth_call", [{"to": token, "data": data}, "latest"]) or "0x0", 16)
