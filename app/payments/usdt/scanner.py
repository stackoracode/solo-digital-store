"""On-chain receipt detection for USDT deposit addresses.

Pipeline per network: (1) chain-id guard, (2) eth_getLogs over [cursor - reorg_depth, head] for
Transfer(to=our addresses) on the single allowed token contract, (3) after N confirmations re-verify
each transfer against its receipt (status, block hash, log content), (4) credit or flag for review,
(5) balanceOf reconciliation to catch logs an RPC/indexer may have omitted.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.payments import (
    ACTIVE_ATTEMPT_STATES,
    ATT_SUCCEEDED,
    TX_CONFIRMED,
    TX_CREDITED,
    TX_FLAGGED,
    TX_REORGED,
    TX_SEEN,
    ChainCursor,
    ChainTransfer,
    CryptoAddress,
    PaymentAttempt,
    PaymentChannel,
)
from app.payments.usdt.networks import Network, get_network
from app.payments.usdt.rpc import TRANSFER_TOPIC, EvmReader, JsonRpcReader, RpcError, topic_address
from app.services.review import open_review

log = logging.getLogger(__name__)
ADDRESS_CHUNK = 50
# Transfers first seen within this grace after attempt expiry are still credited automatically.
LATE_GRACE = timedelta(minutes=5)
MAX_MISSING_CHECKS = 3


def units_to_amount(units: int, decimals: int) -> Decimal:
    return Decimal(units).scaleb(-decimals)


def verify_network(reader: EvmReader, network: Network) -> None:
    chain_id = reader.chain_id()
    if chain_id != network.chain_id:
        raise RpcError(f"{network.key}: RPC chain id {chain_id} != configured {network.chain_id}")
    decimals = reader.token_decimals(network.token_contract)
    if decimals != network.token_decimals:
        raise RpcError(f"{network.key}: token decimals {decimals} != configured {network.token_decimals}")


def scan_network(db: Session, network_key: str, reader: EvmReader | None = None) -> dict[str, int]:
    network = get_network(network_key)
    reader = reader or JsonRpcReader(network)
    stats = {"logs": 0, "confirmed": 0, "credited": 0, "flagged": 0, "reorged": 0}

    chain_id = reader.chain_id()
    if chain_id != network.chain_id:
        raise RpcError(f"chain id mismatch: {chain_id} != {network.chain_id}")
    head = reader.block_number()

    cursor = db.execute(
        select(ChainCursor).where(ChainCursor.network == network.key).with_for_update()
    ).scalar_one_or_none()
    if cursor is None:
        start = network.start_block or max(0, head - network.reorg_depth)
        cursor = ChainCursor(network=network.key, last_scanned_block=start, last_head_block=head)
        db.add(cursor)
        db.flush()

    watched = {
        a.address: a
        for a in db.execute(
            select(CryptoAddress).where(
                CryptoAddress.network == network.key, CryptoAddress.watch_until > utcnow()
            )
        ).scalars()
    }
    if watched:
        from_block = max(network.start_block, cursor.last_scanned_block - network.reorg_depth + 1, 0)
        to_block = min(head, from_block + network.max_block_range - 1)
        addresses = sorted(watched)
        for i in range(0, len(addresses), ADDRESS_CHUNK):
            for entry in reader.get_logs(
                from_block, to_block, network.token_contract, addresses[i : i + ADDRESS_CHUNK]
            ):
                if _record_log(db, network, entry, watched):
                    stats["logs"] += 1
        cursor.last_scanned_block = max(cursor.last_scanned_block, to_block)
    else:
        cursor.last_scanned_block = head
    cursor.last_head_block = head
    cursor.last_error = ""
    cursor.last_success_at = utcnow()
    db.flush()

    _confirm(db, network, reader, head, stats)
    _reconcile_balances(db, network, reader)
    db.commit()
    return stats


def _record_log(db: Session, network: Network, entry: dict, watched: dict[str, CryptoAddress]) -> bool:
    topics = entry.get("topics") or []
    if (entry.get("address") or "").lower() != network.token_contract:
        return False  # wrong token contract (e.g. look-alike "USDT"): never credited
    if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
        return False
    to_addr = topic_address(topics[2])
    if to_addr not in watched:
        return False
    tx_hash = entry["transactionHash"].lower()
    log_index = int(entry["logIndex"], 16)
    block_number = int(entry["blockNumber"], 16)
    block_hash = entry["blockHash"].lower()
    units = int(entry.get("data") or "0x0", 16)
    existing = db.execute(
        select(ChainTransfer)
        .where(
            ChainTransfer.chain_id == network.chain_id,
            ChainTransfer.tx_hash == tx_hash,
            ChainTransfer.log_index == log_index,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if entry.get("removed"):
        if existing is not None and existing.status in (TX_SEEN, TX_CONFIRMED):
            existing.status = TX_REORGED
        return False
    if existing is not None:
        if existing.block_hash != block_hash and existing.status in (TX_SEEN, TX_REORGED):
            existing.block_hash = block_hash
            existing.block_number = block_number
            existing.status = TX_SEEN
            existing.missing_checks = 0
        return False
    db.execute(
        pg_insert(ChainTransfer)
        .values(
            network=network.key,
            chain_id=network.chain_id,
            token_contract=network.token_contract,
            tx_hash=tx_hash,
            log_index=log_index,
            block_number=block_number,
            block_hash=block_hash,
            from_address=topic_address(topics[1]),
            to_address=to_addr,
            amount_units=Decimal(units),
            amount=units_to_amount(units, network.token_decimals),
            status=TX_SEEN,
            attempt_id=watched[to_addr].attempt_id,
            confirmations=0,
            missing_checks=0,
            note="",
        )
        .on_conflict_do_nothing(index_elements=["chain_id", "tx_hash", "log_index"])
    )
    return True


def _receipt_matches(receipt: dict | None, t: ChainTransfer, network: Network) -> str:
    if receipt is None:
        return "missing"
    if (receipt.get("blockHash") or "").lower() != t.block_hash:
        return "reorged"
    if receipt.get("status") != "0x1":
        return "failed_tx"
    for entry in receipt.get("logs") or []:
        if int(entry.get("logIndex", "0x0"), 16) != t.log_index:
            continue
        topics = [x.lower() for x in entry.get("topics") or []]
        ok = (
            (entry.get("address") or "").lower() == network.token_contract
            and len(topics) == 3
            and topics[0] == TRANSFER_TOPIC
            and topic_address(topics[2]) == t.to_address
            and Decimal(int(entry.get("data") or "0x0", 16)) == t.amount_units
        )
        return "ok" if ok else "mismatch"
    return "mismatch"


def _confirm(db: Session, network: Network, reader: EvmReader, head: int, stats: dict[str, int]) -> None:
    pending = (
        db.execute(
            select(ChainTransfer)
            .where(ChainTransfer.network == network.key, ChainTransfer.status == TX_SEEN)
            .order_by(ChainTransfer.block_number, ChainTransfer.log_index)
            .limit(200)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for t in pending:
        t.confirmations = max(0, head - t.block_number + 1)
        if t.confirmations < network.confirmations:
            continue
        verdict = _receipt_matches(reader.get_receipt(t.tx_hash), t, network)
        if verdict == "missing":
            t.missing_checks += 1
            if t.missing_checks >= MAX_MISSING_CHECKS:
                t.status = TX_REORGED
                stats["reorged"] += 1
            continue
        if verdict == "reorged":
            t.status = TX_REORGED  # a re-included copy is picked up again by the overlapping rescan
            stats["reorged"] += 1
            continue
        if verdict != "ok":
            t.status = TX_FLAGGED
            t.note = f"receipt verification failed: {verdict}"
            stats["flagged"] += 1
            open_review(
                db,
                kind="chain_verification_failed",
                transfer_id=t.id,
                attempt_id=t.attempt_id,
                dedupe_key=f"chain_verify:{t.id}",
                summary=f"{network.key} tx {t.tx_hash}: {verdict}",
            )
            continue
        t.status = TX_CONFIRMED
        stats["confirmed"] += 1
        outcome = credit_transfer(db, t)
        stats["credited" if outcome == "credited" else "flagged"] += 1


def credit_transfer(db: Session, t: ChainTransfer) -> str:
    """Apply one confirmed transfer to its attempt. Every ambiguous case goes to manual review."""
    from app.services.payments import apply_payment_success, lock_order

    address = db.execute(
        select(CryptoAddress).where(CryptoAddress.address == t.to_address)
    ).scalar_one_or_none()
    if address is None:
        t.status = TX_FLAGGED
        t.note = "no attempt owns this address"
        open_review(
            db,
            kind="unmatched_transfer",
            transfer_id=t.id,
            dedupe_key=f"unmatched_tx:{t.id}",
            summary=f"Transfer {t.amount} to unknown address {t.to_address}",
        )
        return "flagged"
    attempt = db.get(PaymentAttempt, address.attempt_id)
    assert attempt is not None
    lock_order(db, attempt.order_id)
    attempt = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.id == attempt.id).with_for_update()
    ).scalar_one()
    t.attempt_id = attempt.id

    if attempt.status == ATT_SUCCEEDED:
        t.status = TX_FLAGGED
        t.note = "extra transfer after attempt was paid"
        open_review(
            db,
            kind="extra_transfer",
            transfer_id=t.id,
            attempt_id=attempt.id,
            order_id=attempt.order_id,
            dedupe_key=f"extra_tx:{t.id}",
            summary=f"Extra {t.amount} USDT received for paid attempt {attempt.attempt_no}; refund it",
        )
        return "flagged"
    on_time = attempt.status in ACTIVE_ATTEMPT_STATES and t.created_at <= attempt.expires_at + LATE_GRACE
    if not on_time:
        t.status = TX_FLAGGED
        t.note = f"late transfer (attempt {attempt.status})"
        open_review(
            db,
            kind="late_transfer",
            transfer_id=t.id,
            attempt_id=attempt.id,
            order_id=attempt.order_id,
            dedupe_key=f"late_tx:{t.id}",
            summary=f"{t.amount} USDT arrived for {attempt.status} attempt {attempt.attempt_no}",
        )
        return "flagged"

    t.status = TX_CREDITED
    attempt.received_amount = attempt.received_amount + t.amount
    if attempt.received_amount < attempt.pay_amount:
        db.flush()
        return "credited"  # partial; waits for the remainder or expires into review
    apply_payment_success(
        db,
        attempt.id,
        amount=attempt.received_amount,
        currency="USDT",
        provider_payment_id=None,
        source=f"chain:{t.network}:{t.tx_hash}",
    )
    return "credited"


def _reconcile_balances(db: Session, network: Network, reader: EvmReader) -> None:
    """Detect transfers the log scan missed: on-chain balance higher than recorded transfers."""
    rows = db.execute(
        select(CryptoAddress, PaymentAttempt)
        .join(PaymentAttempt, PaymentAttempt.id == CryptoAddress.attempt_id)
        .where(CryptoAddress.network == network.key, PaymentAttempt.status.in_(ACTIVE_ATTEMPT_STATES))
        .limit(50)
    ).all()
    for address, attempt in rows:
        try:
            balance = reader.balance_of(network.token_contract, address.address)
        except RpcError:
            return
        recorded = db.scalar(
            select(func.coalesce(func.sum(ChainTransfer.amount_units), 0)).where(
                ChainTransfer.to_address == address.address,
                ChainTransfer.status.in_((TX_SEEN, TX_CONFIRMED, TX_CREDITED, TX_FLAGGED)),
            )
        )
        if Decimal(balance) > Decimal(recorded or 0):
            open_review(
                db,
                kind="balance_mismatch",
                severity="medium",
                attempt_id=attempt.id,
                order_id=attempt.order_id,
                dedupe_key=f"balance:{address.address}:{balance}",
                summary=f"{network.key} {address.address} balance {balance} units > recorded {recorded}; "
                "rescan or check explorer",
            )


def mark_channel_error(db: Session, network_key: str, error: str) -> None:
    db.rollback()
    cursor = db.execute(select(ChainCursor).where(ChainCursor.network == network_key)).scalar_one_or_none()
    if cursor is not None:
        cursor.last_error = error[:1000]
    channel = db.execute(
        select(PaymentChannel).where(PaymentChannel.code == f"usdt_{network_key}")
    ).scalar_one_or_none()
    if channel is not None:
        channel.last_error = f"{utcnow():%Y-%m-%d %H:%M} {error}"[:1000]
    db.commit()
