"""Background jobs: a `jobs` row (audit + idempotency) plus a QStash message (retries).

QUEUE_MODE=inline runs the handler in-process immediately (local dev, tests).
Call `enqueue` after your own transaction commits, so the row exists when QStash calls back.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from typing import Any

from app import db
from app.settings import get_settings

log = logging.getLogger("ti.queue")

Handler = Callable[[dict], None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(kind: str, ref_id: str, payload: dict[str, Any] | None = None, *,
            idem: str | None = None, delay_s: int = 0) -> str | None:
    """Creates the job row and dispatches it. Returns the job id, or None if deduplicated."""
    with db.tx() as conn:
        row = db.one(
            conn,
            """insert into jobs (kind, ref_id, payload, idempotency_key)
               values (%s, %s, %s, %s)
               on conflict (idempotency_key) do nothing returning id""",
            (kind, ref_id, db.j(payload or {}), idem),
        )
    if row is None:
        return None
    job_id = str(row["id"])
    dispatch(kind, job_id, delay_s)
    return job_id


INLINE_TRIES = 4  # first try + 3 retries, like QStash


def dispatch(kind: str, job_id: str, delay_s: int = 0) -> None:
    s = get_settings()
    if s.queue_mode == "inline":
        # Mirror QStash's retries=3, so a briefly busy lock doesn't kill the pipeline in dev.
        for attempt in range(INLINE_TRIES):
            if run(job_id) != "failed" or attempt == INLINE_TRIES - 1:
                return
            time.sleep(2 * (attempt + 1))
    from qstash import QStash

    QStash(s.qstash_token).message.publish_json(
        url=f"{s.app_base_url}/api/jobs/{kind}",
        body={"job_id": job_id},
        delay=f"{delay_s}s" if delay_s else None,
        retries=3,
    )


def run(job_id: str) -> str:
    """Runs one job. Safe to call twice: done jobs are skipped. Raises on failure (→ retry)."""
    with db.tx() as conn:
        job = db.one(conn, "select * from jobs where id = %s for update", (job_id,))
        if job is None:
            return "missing"
        if job["status"] == "done":
            return "done"
        conn.execute(
            "update jobs set status='running', attempts=attempts+1, updated_at=now() where id=%s",
            (job_id,),
        )
    fn = HANDLERS.get(job["kind"])
    try:
        if fn is None:
            raise KeyError(f"no handler for {job['kind']}")
        fn(job)
    except Exception as exc:
        log.exception("job %s (%s) failed", job_id, job["kind"])
        with db.tx() as conn:
            conn.execute(
                "update jobs set status='failed', last_error=%s, updated_at=now() where id=%s",
                (f"{exc}\n{traceback.format_exc()[-1500:]}", job_id),
            )
        if get_settings().queue_mode == "inline":
            return "failed"
        raise
    with db.tx() as conn:
        conn.execute("update jobs set status='done', updated_at=now() where id=%s", (job_id,))
    return "done"


def verify_qstash(signature: str | None, body: bytes, url: str) -> bool:
    s = get_settings()
    if s.queue_mode == "inline":
        return True
    if not signature:
        return False
    from qstash import Receiver

    try:
        Receiver(current_signing_key=s.qstash_current_signing_key,
                 next_signing_key=s.qstash_next_signing_key).verify(
            signature=signature, body=body.decode(), url=url)
        return True
    except Exception:
        return False
