"""Worker process: `python -m app.jobs.worker`. Runs jobs and enqueues periodic maintenance jobs."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from types import FrameType

from sqlalchemy import select

from app.config import get_settings
from app.core.logging import configure_logging
from app.db import SessionLocal
from app.jobs.handlers import HANDLERS, Reschedule
from app.jobs.queue import claim, enqueue, mark_failed, mark_succeeded, reschedule
from app.models.ops import Job
from app.models.payments import PaymentChannel

log = logging.getLogger("app.worker")

PERIODIC = {"expire": 60, "reap": 300}
USDT_SCAN_SECONDS = 15


class JobTimeout(Exception):
    pass


def _alarm(signum: int, frame: FrameType | None) -> None:
    raise JobTimeout("job exceeded its timeout")


def run_job(job: Job) -> str:
    """Execute one claimed job with a hard timeout. Returns final status."""
    handler = HANDLERS.get(job.kind)
    db = SessionLocal()
    try:
        if handler is None:
            return mark_failed(db, job.id, f"no handler for {job.kind}", retry_in=3600)
        use_alarm = hasattr(signal, "SIGALRM") and _is_main_thread()
        if use_alarm:
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(max(1, job.timeout_seconds))
        try:
            handler(db, dict(job.payload or {}))
        finally:
            if use_alarm:
                signal.alarm(0)
        db.commit()
        mark_succeeded(db, job.id)
        return "succeeded"
    except Reschedule as r:
        db.rollback()
        reschedule(db, job.id, r.delay, r.note)
        return "rescheduled"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried/dead-lettered
        db.rollback()
        log.exception("job failed", extra={"path": job.kind})
        return mark_failed(db, job.id, f"{exc.__class__.__name__}: {exc}")
    finally:
        db.close()


def _is_main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


def schedule_periodic(now: float) -> None:
    db = SessionLocal()
    try:
        for kind, every in PERIODIC.items():
            enqueue(db, kind, {}, dedupe_key=f"{kind}:{int(now // every)}", max_attempts=3)
        channels = (
            db.execute(
                select(PaymentChannel).where(
                    PaymentChannel.provider == "usdt", PaymentChannel.enabled.is_(True)
                )
            )
            .scalars()
            .all()
        )
        for ch in channels:
            network = ch.config.get("network")
            if network:
                enqueue(
                    db,
                    "usdt_scan",
                    {"network": network},
                    dedupe_key=f"usdt_scan:{network}:{int(now // USDT_SCAN_SECONDS)}",
                    max_attempts=2,
                    timeout_seconds=60,
                )
        db.commit()
    finally:
        db.close()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = False

    def _stop(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started", extra={"host": worker_id})
    last_schedule = 0.0
    while not stopping:
        now = time.time()
        if now - last_schedule >= 5:
            try:
                schedule_periodic(now)
            except Exception:  # noqa: BLE001 - DB hiccup; try again next tick
                log.exception("periodic scheduling failed")
            last_schedule = now
        db = SessionLocal()
        try:
            job = claim(db, worker_id)
        except Exception:  # noqa: BLE001
            log.exception("claim failed")
            job = None
        finally:
            db.close()
        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue
        run_job(job)
    log.info("worker stopped")


def run_until_empty(max_jobs: int = 100) -> int:
    """Synchronously drain due jobs (tests and the CLI)."""
    count = 0
    while count < max_jobs:
        db = SessionLocal()
        try:
            job = claim(db, "inline")
        finally:
            db.close()
        if job is None:
            break
        run_job(job)
        count += 1
    return count


if __name__ == "__main__":
    main()
