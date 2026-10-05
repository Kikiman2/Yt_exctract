"""Recurring jobs: feed refresh (interval from settings, plus once shortly after start),
automatic retry of failed downloads, retention cleanup, health checks."""

from __future__ import annotations

import datetime
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .services import downloads, health, retention, subscriptions

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

REFRESH_JOB_ID = "refresh_all"
STARTUP_DELAY_SECONDS = 5


def start(poll_minutes: int) -> None:
    common = {"replace_existing": True, "coalesce": True, "max_instances": 1}
    now = datetime.datetime.now(datetime.UTC)
    scheduler.add_job(subscriptions.refresh_all, "interval", minutes=poll_minutes,
                      id=REFRESH_JOB_ID, **common)
    # A short delay so the app is serving (and health has run) before the first refresh.
    scheduler.add_job(subscriptions.refresh_all, "date",
                      run_date=now + datetime.timedelta(seconds=STARTUP_DELAY_SECONDS),
                      id="refresh_all_startup", **common)
    scheduler.add_job(downloads.retry_due, "interval", minutes=10, id="retry_due", **common)
    scheduler.add_job(retention.cleanup, "interval", hours=6, id="cleanup", **common)
    scheduler.add_job(health.check_now, "interval", minutes=5, id="health", **common)
    scheduler.start()


def reschedule_poll(minutes: int) -> None:
    """No-op before start() (settings can be saved while the scheduler isn't running)."""
    if scheduler.running and scheduler.get_job(REFRESH_JOB_ID):
        scheduler.reschedule_job(REFRESH_JOB_ID, trigger="interval", minutes=minutes)


def shutdown() -> None:
    global scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
    # A stopped AsyncIOScheduler stays bound to its old event loop and its shutdown only
    # lands on the next loop turn, so start() after a shutdown (tests, reloads) needs a
    # fresh instance.
    scheduler = AsyncIOScheduler()
