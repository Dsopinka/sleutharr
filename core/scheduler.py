"""In-process polling via APScheduler.

One container, one process, no broker. A single background scheduler with a small thread
pool runs the poll cycle; `BaseClient` enforces one in-flight request per service, so
concurrency here never translates into hammering any single upstream.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from django.db import close_old_connections

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()

POLL_JOB_ID = "poll-cycle"

# The scheduler is only a heartbeat. Actual pacing is per-service: `due_services()`
# skips anything whose own poll_interval has not elapsed. Keeping this a constant means
# start_scheduler() touches no database during app initialisation, which Django warns
# about (and which would break if the DB is not migrated yet on a fresh container).
HEARTBEAT_SECONDS = 15


def _run_poll_cycle() -> None:
    """APScheduler entry point.

    Django connections are thread-local and SQLite connections go stale; closing old ones
    at the boundary avoids "database is locked" surprises after an idle period.
    """
    close_old_connections()
    try:
        from core.ingest import run_poll_cycle

        run_poll_cycle()
    except Exception:  # noqa: BLE001 - a failing cycle must not kill the scheduler
        logger.exception("Poll cycle failed")
    finally:
        close_old_connections()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return _scheduler

        scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                # If a cycle overruns, skip rather than pile up.
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )
        scheduler.add_job(
            _run_poll_cycle,
            "interval",
            seconds=HEARTBEAT_SECONDS,
            id=POLL_JOB_ID,
            next_run_time=None,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info("Scheduler started; heartbeat every %ss", HEARTBEAT_SECONDS)

        # Kick one cycle shortly after boot so a fresh install populates without waiting.
        scheduler.modify_job(POLL_JOB_ID, next_run_time=_soon())
        return scheduler


def _soon():
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() + timedelta(seconds=10)


def run_all_now() -> None:
    """Trigger a cycle immediately (used by the 'Poll now' button)."""
    scheduler = _scheduler
    if scheduler is None:
        # Scheduler disabled (tests, or SLEUTHARR_SCHEDULER=0): run inline.
        _run_poll_cycle()
        return
    scheduler.modify_job(POLL_JOB_ID, next_run_time=_soon())


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
