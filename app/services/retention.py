"""Periodic housekeeping: delete expired downloads, prune the events log, remove
staging dirs left behind by a crash."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

from .. import config, db
from ..domain import retention
from ..models import LibraryOffline
from . import downloads
from . import settings as settings_service
from .context import ctx

logger = logging.getLogger(__name__)

MAX_EVENTS = 5000
STAGING_MAX_AGE = 24 * 3600


def _prune_staging(active: set[str]) -> int:
    root = config.STAGING_DIR
    if not root.exists():
        return 0
    cutoff = time.time() - STAGING_MAX_AGE
    removed = 0
    for d in root.iterdir():
        try:
            if d.name not in active and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


async def cleanup() -> int:
    """Returns the number of videos deleted by retention."""
    stored = await db.get_settings()
    global_days = settings_service.as_int(stored, "auto_delete_days")
    now_iso = db.now()
    c = ctx()
    deleted = 0
    for row in await db.list_downloaded():
        if not retention.is_expired(row, global_days, now_iso):
            continue
        try:
            if row["file_path"]:
                await asyncio.to_thread(c.writer.delete, row["file_path"])
                c.notifier.touch(row["file_path"].rpartition("/")[0])
            await db.update_video(row["video_id"], {
                "status": "deleted", "deleted_at": now_iso, "file_path": None, "file_size": None})
            await db.add_event("info", "retention", "Expired, file deleted", row["video_id"])
            deleted += 1
        except LibraryOffline:
            break  # nothing may be touched; the next run tries again
        except Exception:  # noqa: BLE001 - one bad file must not stop the rest
            logger.exception("Retention delete of %s failed", row["video_id"])
    await db.prune_events(MAX_EVENTS)
    await asyncio.to_thread(_prune_staging, downloads.active_ids())
    return deleted
