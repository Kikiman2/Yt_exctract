import asyncio
import datetime
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, downloader, youtube

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

JOB_ID = "poll_channels"
CLEANUP_JOB_ID = "cleanup_old_downloads"
CLEANUP_INTERVAL_HOURS = 6


async def poll_all_channels() -> None:
    channels = await db.list_enabled_channels()
    for channel in channels:
        channel_id = channel["channel_id"]
        try:
            videos = await asyncio.to_thread(youtube.fetch_latest_videos, channel_id)
            for video_id, title in videos:
                await db.add_pending_video(video_id, channel_id, title)
            await db.touch_channel_checked(channel_id)
        except Exception:  # noqa: BLE001 - one channel failing shouldn't stop the rest
            logger.exception("Failed to poll channel %s", channel_id)


async def cleanup_old_downloads() -> None:
    """Delete downloaded files (and mark them as such) once they're older than the
    configured retention period. Disabled when auto_delete_days is 0."""
    settings = await db.get_settings()
    try:
        days = int(settings.get("auto_delete_days", "0"))
    except ValueError:
        days = 0
    if days <= 0:
        return

    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    videos = await db.list_stale_downloaded_videos(cutoff)
    for video in videos:
        try:
            await asyncio.to_thread(downloader.delete_downloaded_file, video["file_path"])
            await db.set_video_status(video["video_id"], "deleted")
        except Exception:  # noqa: BLE001 - one bad video shouldn't stop the rest
            logger.exception("Failed to auto-delete video %s", video["video_id"])


def start(interval_minutes: int) -> None:
    scheduler.add_job(
        poll_all_channels,
        "interval",
        minutes=interval_minutes,
        id=JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup_old_downloads,
        "interval",
        hours=CLEANUP_INTERVAL_HOURS,
        id=CLEANUP_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()


def reschedule(interval_minutes: int) -> None:
    scheduler.reschedule_job(JOB_ID, trigger="interval", minutes=interval_minutes)
