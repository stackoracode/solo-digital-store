"""PostgreSQL-backed durable job queue (transactional outbox + worker in one table).

enqueue() only adds a row to the *caller's* session, so the job commits or rolls back together with
the business change that produced it. Workers claim rows with FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.ops import JOB_DEAD, JOB_QUEUED, JOB_RUNNING, JOB_SUCCEEDED, Job

log = logging.getLogger(__name__)

# Retry backoff in seconds by attempt number (1-based); last value repeats.
BACKOFF = [5, 15, 60, 180, 600, 1800, 3600]


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 8,
    timeout_seconds: int = 60,
) -> None:
    """Insert a job in the current transaction. With dedupe_key, a duplicate enqueue is a no-op."""
    values = {
        "kind": kind,
        "payload": payload or {},
        "status": JOB_QUEUED,
        "dedupe_key": dedupe_key,
        "attempts": 0,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
        "run_at": utcnow() + timedelta(seconds=delay_seconds),
        "last_error": "",
    }
    stmt = pg_insert(Job).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    db.execute(stmt)


def claim(db: Session, worker_id: str) -> Job | None:
    """Claim one due job. Commits the claim so other workers skip it."""
    job = db.execute(
        select(Job)
        .where(Job.status == JOB_QUEUED, Job.run_at <= func.now())
        .order_by(Job.run_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        db.rollback()
        return None
    job.status = JOB_RUNNING
    job.locked_at = utcnow()
    job.locked_by = worker_id
    job.attempts += 1
    db.commit()
    return job


def mark_succeeded(db: Session, job_id: int) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status=JOB_SUCCEEDED, finished_at=func.now(), locked_at=None, last_error="")
    )
    db.commit()


def mark_failed(db: Session, job_id: int, error: str, *, retry_in: float | None = None) -> str:
    job = db.get(Job, job_id, with_for_update=True)
    if job is None:
        return "missing"
    job.last_error = error[:4000]
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JOB_DEAD
        job.finished_at = utcnow()
        _dead_letter_review(db, job)
    else:
        delay = retry_in if retry_in is not None else BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
        job.status = JOB_QUEUED
        job.run_at = utcnow() + timedelta(seconds=delay)
    db.commit()
    return job.status


def reschedule(db: Session, job_id: int, delay_seconds: float, note: str = "") -> None:
    """Re-queue without consuming the retry budget (e.g. "still waiting for upstream")."""
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JOB_QUEUED,
            run_at=utcnow() + timedelta(seconds=delay_seconds),
            attempts=Job.attempts - 1,
            locked_at=None,
            last_error=note,
        )
    )
    db.commit()


def reap_stuck(db: Session, now: datetime | None = None) -> int:
    """Running jobs whose lock outlived their timeout (worker crashed) go back to the queue."""
    now = now or utcnow()
    rows = (
        db.execute(select(Job).where(Job.status == JOB_RUNNING).with_for_update(skip_locked=True))
        .scalars()
        .all()
    )
    count = 0
    for job in rows:
        if job.locked_at and job.locked_at + timedelta(seconds=job.timeout_seconds * 2 + 30) < now:
            job.status = JOB_QUEUED if job.attempts < job.max_attempts else JOB_DEAD
            job.last_error = (job.last_error + "\nreaped after worker timeout").strip()
            job.locked_at = None
            job.run_at = now
            if job.status == JOB_DEAD:
                _dead_letter_review(db, job)
            count += 1
    db.commit()
    return count


def retry_dead(db: Session, job_id: int) -> bool:
    job = db.get(Job, job_id, with_for_update=True)
    if job is None or job.status not in (JOB_DEAD, "canceled"):
        return False
    job.status = JOB_QUEUED
    job.attempts = 0
    job.run_at = utcnow()
    job.finished_at = None
    db.commit()
    return True


def _dead_letter_review(db: Session, job: Job) -> None:
    from app.services.review import open_review

    open_review(
        db,
        kind="job_dead",
        summary=f"Job {job.kind} #{job.id} exhausted retries: {job.last_error[:200]}",
        dedupe_key=f"job_dead:{job.id}:{job.attempts}",
        data={"job_id": job.id, "kind": job.kind, "payload": job.payload},
        order_id=job.payload.get("order_id") if isinstance(job.payload, dict) else None,
    )
