import functools
import logging
import time
import traceback
from datetime import UTC, datetime

import orjson

from albatross import controls, db, keys
from albatross.http import redact

log = logging.getLogger(__name__)


async def record_run(name: str, started: datetime, seconds: float | None, status: str, rows: int | None = None,
                     detail: dict | None = None, error: str | None = None) -> None:
    """One job_runs row (the Output page's history). Never raises: tracking must not break a job."""
    try:
        pool = await db.pool()
        await pool.execute(
            "INSERT INTO job_runs (job, started_at, seconds, status, rows, detail, error) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            name, started, seconds, status, rows, orjson.dumps(detail).decode() if detail else None, error)
    except Exception:  # noqa: BLE001
        log.debug("job_runs insert failed for %s", name, exc_info=True)


def tracked(name: str):
    """Record every run of a job in ingest_health (latest state) and job_runs (history); skip it while it is
    paused on the Output page; never let one job crash the scheduler. A job returns rows written, or a dict with
    "rows" and its own breakdown (e.g. {"rows": 12, "new": 3}), kept in job_runs.detail."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            started, t0 = datetime.now(UTC), time.monotonic()
            pool = await db.pool()
            if await controls.paused(name):
                await record_run(name, started, 0.0, "paused")
                log.info("job %s paused on the Output page; skipped", name)
                return 0
            try:
                out = await fn(*args, **kwargs)
            except keys.NotConfigured as e:
                # Nothing to run with yet (no key, no plan): a skip, not a failure, so a fresh install raises no alerts.
                await pool.execute(
                    """INSERT INTO ingest_health (job, last_run, last_ok, consecutive_errors, last_rows)
                       VALUES ($1, now(), now(), 0, 0)
                       ON CONFLICT (job) DO UPDATE SET last_run = now(), last_ok = now(), consecutive_errors = 0,
                         last_rows = 0""", name)
                await record_run(name, started, time.monotonic() - t0, "skipped", error=redact(str(e))[:500])
                log.debug("job %s skipped: %s", name, e)
                return 0
            except Exception as e:  # noqa: BLE001 - recorded and surfaced on the dashboard
                # redacted: a provider's error text can carry a key from its URL, and this goes to the log, the
                # Health page, the alerts and the CEO memo's prompt
                log.error("job %s failed\n%s", name, redact("".join(traceback.format_exception(e)).rstrip()))
                err = redact(f"{type(e).__name__}: {e}")[:500]
                await pool.execute(
                    """INSERT INTO ingest_health (job, last_run, last_error, consecutive_errors)
                       VALUES ($1, now(), $2, 1)
                       ON CONFLICT (job) DO UPDATE SET last_run = now(), last_error = $2,
                         consecutive_errors = ingest_health.consecutive_errors + 1""",
                    name, err,
                )
                await record_run(name, started, time.monotonic() - t0, "failed", error=err)
                return None
            rows, detail = (out.get("rows"), out) if isinstance(out, dict) else (out, None)
            await pool.execute(
                """INSERT INTO ingest_health (job, last_run, last_ok, consecutive_errors, last_rows)
                   VALUES ($1, now(), now(), 0, $2)
                   ON CONFLICT (job) DO UPDATE SET last_run = now(), last_ok = now(),
                     consecutive_errors = 0, last_rows = $2""",
                name, rows,
            )
            await record_run(name, started, time.monotonic() - t0, "ok", rows, detail)
            log.info("job %s ok: %s rows in %.1fs", name, rows, time.monotonic() - t0)
            return rows

        # APScheduler names a job after its function: our job name makes its logs and events readable.
        wrapper.__name__ = wrapper.__qualname__ = name
        return wrapper

    return deco
