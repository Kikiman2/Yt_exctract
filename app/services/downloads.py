"""Download pipeline: worker pool, per-video state machine, pause reasons.

videos.status is the source of truth:
    pending -> downloading -> downloaded | skipped | failed
    failed -> pending (auto retry after next_retry_at, or by hand)
A video is claimed atomically by db.claim_next(); everything a download writes goes to
STAGING_DIR/<video_id>, is moved into the library, and the staging dir is removed on
every path (success, skip, failure, cancel)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import shutil

from .. import config, db
from ..domain import naming, retention
from ..library import nfo
from ..models import DownloadRequest, LibraryOffline, ProgressInfo, VIDEO_STATUSES, YouTubeError
from . import settings as settings_service
from .context import ctx

logger = logging.getLogger(__name__)

# Seconds an idle or paused worker sleeps before looking again (tests shrink it).
IDLE_POLL = 2.0

LIBRARY_OFFLINE = "Library offline"
USER_PAUSED = "Paused by user"
BOT_HINT = " (YouTube asked for a login: upload fresh cookies under Settings)"

_workers: dict[int, asyncio.Task] = {}
_target = 0
_started = False
_progress: dict[str, ProgressInfo] = {}
# Reasons set by health checks (plus LibraryOffline seen mid-install).
_health_reasons: list[str] = []


# --- pause reasons ----------------------------------------------------------------


def set_health_reasons(reasons: list[str]) -> None:
    _health_reasons[:] = list(reasons)


async def disk_free_gb() -> float | None:
    def free() -> float:
        config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(config.STAGING_DIR).free / 1024**3

    try:
        return await asyncio.to_thread(free)
    except OSError:
        return None


async def pause_reasons() -> list[str]:
    """Why no new download is being started right now (empty = running)."""
    stored = await db.get_settings()
    reasons: list[str] = []
    if settings_service.as_bool(stored, "pipeline_paused"):
        reasons.append(USER_PAUSED)
    reasons.extend(r for r in _health_reasons if r not in reasons)
    min_gb = settings_service.as_int(stored, "min_free_gb")
    if min_gb > 0:
        free = await disk_free_gb()
        if free is not None and free < min_gb:
            reasons.append(f"Low disk space ({free:.1f} GB free, minimum {min_gb} GB)")
    return reasons


async def pause() -> None:
    await db.set_setting("pipeline_paused", "true")
    await db.add_event("info", "pipeline", "Downloads paused")


async def resume() -> None:
    await db.set_setting("pipeline_paused", "false")
    await db.add_event("info", "pipeline", "Downloads resumed")


# --- JSON shapes ------------------------------------------------------------------


def get_progress(video_id: str) -> dict | None:
    p = _progress.get(video_id)
    if p is None:
        return None
    return {"status": p.status, "percent": p.percent, "speed": p.speed, "eta": p.eta}


def active_ids() -> set[str]:
    """Videos with a live download (their staging dirs must not be touched)."""
    return set(_progress)


def video_json(row: dict) -> dict:
    """VideoJson from a joined videos row."""
    vid = row["video_id"]
    return {
        "video_id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": row["title"],
        "source_id": row["channel_id"],
        "channel_name": row.get("channel_name"),
        "status": row["status"],
        "error": row["error"],
        "attempts": row["attempts"],
        "next_retry_at": row["next_retry_at"],
        "added_at": row["added_at"],
        "downloaded_at": row["downloaded_at"],
        "file_path": row["file_path"],
        "file_size": row["file_size"],
        "upload_date": row["upload_date"],
        "duration": row["duration"],
        "progress": get_progress(vid),
    }


async def queue_status() -> dict:
    reasons = await pause_reasons()
    counts = {s: 0 for s in VIDEO_STATUSES}
    counts.update(await db.count_by_status())
    active = await db.list_videos_by_status("downloading")
    return {
        "paused": bool(reasons),
        "pause_reasons": reasons,
        "counts": counts,
        "active": [video_json(r) for r in active],
    }


async def retry_due() -> int:
    """Scheduler job: failed videos whose backoff has passed go back to pending."""
    stored = await db.get_settings()
    n = await db.requeue_failed_due(settings_service.as_int(stored, "max_attempts"))
    if n:
        await db.add_event("info", "retry", f"{n} failed download(s) queued for another attempt")
    return n


# --- worker pool ------------------------------------------------------------------


async def _worker(index: int) -> None:
    # A worker whose index is >= the target exits once idle: that is how the pool shrinks.
    while index < _target:
        try:
            if await pause_reasons():
                await asyncio.sleep(IDLE_POLL)
                continue
            video = await db.claim_next()
        except Exception:  # noqa: BLE001 - keep the worker alive through a transient DB error
            logger.exception("Worker %d failed to claim a video", index)
            await asyncio.sleep(IDLE_POLL)
            continue
        if video is None:
            await asyncio.sleep(IDLE_POLL)
            continue
        await process_one(video)


def _spawn_missing() -> None:
    for i in range(_target):
        task = _workers.get(i)
        if task is None or task.done():
            _workers[i] = asyncio.create_task(_worker(i), name=f"download-worker-{i}")


def set_concurrency(n: int) -> None:
    """Applies live: extra workers start now, surplus ones stop after their current video."""
    global _target
    _target = max(1, n)
    if _started:
        _spawn_missing()


async def start() -> None:
    global _started
    stored = await db.get_settings()
    _started = True
    set_concurrency(settings_service.as_int(stored, "max_concurrent_downloads"))


async def stop() -> None:
    global _started, _target
    _started = False
    _target = 0
    tasks = list(_workers.values())
    _workers.clear()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- one video --------------------------------------------------------------------


def _hours_from_now(hours: int) -> str:
    when = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(hours=hours)
    return when.isoformat(timespec="seconds")


def _override(row_value, global_value: bool) -> bool:
    return global_value if row_value is None else bool(row_value)


async def _fail(video: dict, message: str, stored: dict[str, str]) -> None:
    attempts = video["attempts"] + 1
    max_attempts = settings_service.as_int(stored, "max_attempts")
    next_retry = None
    if attempts < max_attempts:
        hours = retention.backoff_hours(attempts, settings_service.as_int(stored, "retry_failed_after_hours"))
        next_retry = _hours_from_now(hours)
    await db.update_video(video["video_id"], {
        "status": "failed", "error": message[:1000], "attempts": attempts,
        "next_retry_at": next_retry, "started_at": None,
    })
    suffix = f" (attempt {attempts}/{max_attempts}" + (f", retrying after {next_retry} UTC)" if next_retry else ", giving up)")
    await db.add_event("error", "download_failed", message[:500] + suffix, video["video_id"])


async def _write_sidecars(meta, channel: str | None, rel_folder: str, file_name: str,
                          thumbnail) -> None:
    writer = ctx().writer
    names = naming.sidecar_names(file_name)
    await asyncio.to_thread(
        writer.write_bytes, rel_folder, names["nfo"], nfo.render_nfo(meta, channel))
    if thumbnail is not None:
        poster = await asyncio.to_thread(nfo.poster_bytes, thumbnail)
        if poster:
            await asyncio.to_thread(writer.write_bytes, rel_folder, names["poster"], poster)


async def process_one(video: dict) -> None:
    """Download one claimed video. Never raises (except cancellation): every outcome is
    recorded on the row and in the events log."""
    vid = video["video_id"]
    stored = await db.get_settings()
    c = ctx()
    staging = config.STAGING_DIR / vid
    try:
        await asyncio.to_thread(c.writer.assert_online)
        await asyncio.to_thread(shutil.rmtree, staging, True)  # leftovers of a crashed attempt
        staging.mkdir(parents=True, exist_ok=True)
        req = DownloadRequest(
            video_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            staging_dir=staging,
            quality=video["sub_quality"] or stored.get("quality", "best"),
            skip_shorts=_override(video["sub_skip_shorts"], settings_service.as_bool(stored, "skip_shorts")),
            shorts_max_seconds=settings_service.as_int(stored, "shorts_max_seconds"),
            skip_live=_override(video["sub_skip_live"], settings_service.as_bool(stored, "skip_live")),
            force=bool(video["force_download"]),
        )

        def on_progress(info: ProgressInfo) -> None:  # runs in the download thread
            # A cancelled download's thread can still report after cleanup; don't resurrect it.
            if vid in _progress:
                _progress[vid] = info

        _progress[vid] = ProgressInfo("downloading")
        result = await asyncio.to_thread(c.youtube.download, req, on_progress)

        if result.skip_reason:
            await db.update_video(vid, {
                "status": "skipped", "error": result.skip_reason, "started_at": None,
                "duration": result.meta.duration if result.meta else None,
            })
            await db.add_event("info", "skipped", result.skip_reason, vid)
            return

        _progress[vid] = ProgressInfo("processing", 100.0)
        meta = result.meta
        title = (meta.title if meta and meta.title else video["title"])
        channel = video["channel_name"] or (meta.channel if meta else None)
        file_name = naming.video_file_name(title, vid)
        rel_folder = naming.rel_folder(stored.get("download_subfolder", "YouTube"), channel)
        installed = await asyncio.to_thread(c.writer.install, rel_folder, file_name, result.video)

        if meta and settings_service.as_bool(stored, "write_nfo"):
            try:
                await _write_sidecars(meta, channel, rel_folder, file_name, result.thumbnail)
            except LibraryOffline:
                raise
            except Exception as e:  # noqa: BLE001 - the video is in place; sidecars are a bonus
                logger.warning("Sidecars for %s failed: %s", vid, e)
                await db.add_event("warning", "sidecar", f"NFO/poster not written: {e}", vid)

        c.notifier.touch(rel_folder)
        await db.update_video(vid, {
            "status": "downloaded", "downloaded_at": db.now(), "file_path": installed.rel_path,
            "file_size": installed.size, "error": None, "next_retry_at": None, "started_at": None,
            "title": title,
            "upload_date": meta.upload_date if meta else None,
            "duration": meta.duration if meta else None,
            "channel_title": channel,
        })
        await db.add_event("info", "downloaded", installed.rel_path, vid)
    except LibraryOffline:
        # Not the video's fault: back in the queue, no attempt consumed, pipeline paused.
        await db.update_video(vid, {"status": "pending", "started_at": None})
        if LIBRARY_OFFLINE not in _health_reasons:
            _health_reasons.append(LIBRARY_OFFLINE)
            await db.add_event("warning", "library_offline",
                               "Library sentinel missing: downloads paused", vid)
    except YouTubeError as e:
        await _fail(video, str(e) + (BOT_HINT if e.bot_check else ""), stored)
    except asyncio.CancelledError:
        await db.update_video(vid, {"status": "pending", "started_at": None})
        raise
    except Exception as e:  # noqa: BLE001 - one bad video must not kill the worker
        logger.exception("Download of %s failed", vid)
        await _fail(video, f"{type(e).__name__}: {e}", stored)
    finally:
        _progress.pop(vid, None)
        await asyncio.to_thread(shutil.rmtree, staging, True)
