"""USDT on-chain receipt verification against a fake EVM node (no real assets, no network)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip39WordsNum,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)
from sqlalchemy import func, select

from app.db import utcnow
from app.models.ops import ReviewItem
from app.models.orders import Delivery, Order
from app.models.payments import ChainTransfer, PaymentAttempt
from app.payments.usdt.hd import XpubError, derive_address
from app.payments.usdt.networks import get_network
from app.payments.usdt.rpc import TRANSFER_TOPIC, address_topic
from app.payments.usdt.scanner import scan_network
from tests.conftest import drain, enable_channel, make_order, make_product, start

NET = "xlayer"


def _account_keys():
    mnemonic = Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)
    seed = Bip39SeedGenerator(mnemonic).Generate()
    account = Bip44.FromSeed(seed, Bip44Coins.ETHEREUM).Purpose().Coin().Account(0)
    return account, account.PublicKey().ToExtended()


@pytest.fixture
def xpub(monkeypatch):
    from app.config import get_settings
    from app.payments.usdt import hd

    hd._account.cache_clear()
    _, xpub = _account_keys()
    monkeypatch.setattr(get_settings(), "usdt_xpub", xpub)
    return xpub


class FakeChain:
    """Minimal EVM reader double: logs, receipts, head, balances."""

    def __init__(self, chain_id: int = 196, decimals: int = 6) -> None:
        self.net = get_network(NET)
        self._chain_id = chain_id
        self._decimals = decimals
        self.head = 1000
        self.logs: list[dict] = []
        self.receipts: dict[str, dict | None] = {}
        self.balances: dict[str, int] = {}

    def add_transfer(
        self,
        to: str,
        units: int,
        *,
        block: int,
        tx: str,
        log_index: int = 0,
        token: str | None = None,
        block_hash: str | None = None,
    ) -> dict:
        token = token or self.net.token_contract
        block_hash = block_hash or "0x" + f"{block:064x}"
        log = {
            "address": token,
            "topics": [TRANSFER_TOPIC, address_topic("0x" + "11" * 20), address_topic(to)],
            "data": hex(units),
            "blockNumber": hex(block),
            "blockHash": block_hash,
            "transactionHash": tx,
            "logIndex": hex(log_index),
        }
        self.logs.append(log)
        self.receipts[tx] = {"status": "0x1", "blockHash": block_hash, "logs": [log]}
        if token == self.net.token_contract:
            self.balances[to] = self.balances.get(to, 0) + units
        return log

    # EvmReader protocol
    def chain_id(self) -> int:
        return self._chain_id

    def block_number(self) -> int:
        return self.head

    def get_logs(self, from_block, to_block, token, to_addresses):
        topics = {address_topic(a) for a in to_addresses}
        return [
            log
            for log in self.logs
            if from_block <= int(log["blockNumber"], 16) <= to_block and log["topics"][2] in topics
            # a real node filters by contract; return everything to prove the scanner re-checks it
        ]

    def get_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    def token_decimals(self, token):
        return self._decimals

    def balance_of(self, token, address):
        return self.balances.get(address, 0)


def _usdt_order(db, store, price: str = "10.00"):
    enable_channel(db, f"usdt_{NET}")
    product = make_product(db, store, price=price, codes=3)
    order, _ = make_order(db, store, product)
    attempt = start(db, order, f"usdt_{NET}")
    return order, attempt


def _units(amount: str) -> int:
    return int(Decimal(amount) * 10**6)


def test_xpub_derivation_matches_private_derivation():
    account, xpub = _account_keys()
    for i in (0, 1, 7):
        expected = account.Change(Bip44Changes.CHAIN_EXT).AddressIndex(i).PublicKey().ToAddress().lower()
        assert derive_address(xpub, i) == expected
    with pytest.raises(XpubError):
        derive_address(account.PrivateKey().ToExtended(), 0)


def test_unique_address_per_attempt_and_exact_amount(db, store, xpub):
    o1, a1 = _usdt_order(db, store, "10.00")
    o2, a2 = _usdt_order(db, store, "10.00")
    assert a1.extra["address"] != a2.extra["address"]
    assert a1.pay_amount == Decimal("10.00") and a1.pay_currency == "USDT"  # no artificial decimals
    assert a1.fx_rate == Decimal("1") and a1.fx_source.startswith("peg")


def test_confirmed_transfer_credits_and_delivers(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    chain.add_transfer(attempt.extra["address"], _units("10.00"), block=990, tx="0x" + "a" * 64)
    chain.head = 995  # 6 confirmations < 20 required
    scan_network(db, NET, chain)
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    chain.head = 1010
    scan_network(db, NET, chain)
    drain()
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.payment_status == "paid" and order.fulfillment_status == "delivered"


def test_duplicate_log_is_credited_once(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    chain.add_transfer(attempt.extra["address"], _units("10.00"), block=980, tx="0x" + "b" * 64)
    chain.head = 1010
    scan_network(db, NET, chain)
    scan_network(db, NET, chain)  # overlapping rescan returns the same log again
    chain.logs.append(dict(chain.logs[0]))  # and a node returning it twice in one response
    scan_network(db, NET, chain)
    assert db.scalar(select(func.count()).select_from(ChainTransfer)) == 1
    db.expire_all()
    assert db.get(PaymentAttempt, attempt.id).received_amount == Decimal("10.00")


def test_wrong_token_contract_is_ignored(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    fake_usdt = "0x" + "de" * 20  # look-alike token named "USDT"
    chain.add_transfer(
        attempt.extra["address"], _units("10.00"), block=980, tx="0x" + "c" * 64, token=fake_usdt
    )
    chain.head = 1010
    scan_network(db, NET, chain)
    assert db.scalar(select(func.count()).select_from(ChainTransfer)) == 0
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_reorg_before_confirmation_is_not_credited(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    tx = "0x" + "d" * 64
    chain.add_transfer(attempt.extra["address"], _units("10.00"), block=990, tx=tx)
    chain.head = 995
    scan_network(db, NET, chain)
    # Chain reorganises: the block is replaced and the transaction disappears.
    chain.logs.clear()
    chain.receipts[tx] = None
    chain.head = 1015
    for _ in range(3):
        scan_network(db, NET, chain)
    transfer = db.execute(select(ChainTransfer)).scalar_one()
    assert transfer.status == "reorged"
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_block_hash_mismatch_marks_reorged(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    tx = "0x" + "e" * 64
    chain.add_transfer(attempt.extra["address"], _units("10.00"), block=990, tx=tx)
    chain.head = 995
    scan_network(db, NET, chain)
    chain.receipts[tx] = {**chain.receipts[tx], "blockHash": "0x" + "f" * 64}
    chain.logs.clear()
    chain.head = 1015
    scan_network(db, NET, chain)
    assert db.execute(select(ChainTransfer)).scalar_one().status == "reorged"
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_underpayment_then_topup(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    addr = attempt.extra["address"]
    chain.add_transfer(addr, _units("6.00"), block=980, tx="0x" + "1" * 64)
    chain.head = 1005
    scan_network(db, NET, chain)
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    assert db.get(PaymentAttempt, attempt.id).received_amount == Decimal("6.00")
    chain.add_transfer(addr, _units("4.00"), block=1001, tx="0x" + "2" * 64)
    chain.head = 1030
    scan_network(db, NET, chain)
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "paid"


def test_underpaid_attempt_expires_into_review(db, store, xpub):
    from app.services.payments import expire_attempts

    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    chain.add_transfer(attempt.extra["address"], _units("3.00"), block=980, tx="0x" + "3" * 64)
    chain.head = 1010
    scan_network(db, NET, chain)
    a = db.get(PaymentAttempt, attempt.id)
    a.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    expire_attempts(db)
    db.expire_all()
    assert db.get(PaymentAttempt, attempt.id).status == "needs_review"
    drain()
    assert db.scalar(select(func.count()).select_from(Delivery)) == 0


def test_late_and_extra_transfers_go_to_review(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    addr = attempt.extra["address"]
    chain.add_transfer(addr, _units("10.00"), block=980, tx="0x" + "4" * 64)
    chain.head = 1010
    scan_network(db, NET, chain)
    # The customer pays a second time to the same address.
    chain.add_transfer(addr, _units("10.00"), block=1005, tx="0x" + "5" * 64)
    chain.head = 1040
    scan_network(db, NET, chain)
    kinds = set(db.execute(select(ReviewItem.kind)).scalars())
    assert "extra_transfer" in kinds
    drain()
    assert db.scalar(select(func.count()).select_from(Delivery)) == 1


def test_transfer_to_expired_attempt_is_not_auto_credited(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    a = db.get(PaymentAttempt, attempt.id)
    a.status = "expired"
    a.expires_at = utcnow() - timedelta(hours=2)
    db.commit()
    chain = FakeChain()
    chain.add_transfer(attempt.extra["address"], _units("10.00"), block=980, tx="0x" + "6" * 64)
    chain.head = 1010
    scan_network(db, NET, chain)
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"
    assert (
        db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.kind == "late_transfer")) == 1
    )


def test_chain_id_mismatch_aborts_scan(db, store, xpub):
    from app.payments.usdt.rpc import RpcError

    _usdt_order(db, store)
    with pytest.raises(RpcError):
        scan_network(db, NET, FakeChain(chain_id=1))


def test_balance_reconciliation_flags_missed_logs(db, store, xpub):
    order, attempt = _usdt_order(db, store)
    chain = FakeChain()
    chain.balances[attempt.extra["address"]] = _units("10.00")  # funds arrived but the node returned no log
    scan_network(db, NET, chain)
    assert (
        db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.kind == "balance_mismatch"))
        == 1
    )
    db.expire_all()
    assert db.get(Order, order.id).payment_status == "unpaid"


def test_network_verification_checks_decimals():
    from app.payments.usdt.rpc import RpcError
    from app.payments.usdt.scanner import verify_network

    net = get_network(NET)
    verify_network(FakeChain(), net)
    with pytest.raises(RpcError):
        verify_network(FakeChain(decimals=18), net)
