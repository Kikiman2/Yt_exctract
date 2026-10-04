import asyncio
import logging
import re
from pathlib import Path

import yt_dlp

from . import config, db

logger = logging.getLogger(__name__)

# video_id -> {status, percent, speed, eta}
progress_state: dict[str, dict] = {}

_INVALID_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_folder_name(name: str) -> str:
    name = _INVALID_FOLDER_CHARS.sub("_", name).strip()
    return name or "Unknown"


def delete_downloaded_file(file_path: str | None) -> None:
    """Best-effort removal of a downloaded file plus any sidecar files (thumbnail,
    subtitles, etc.) sharing the same stem. Missing files are silently ignored
    since they may have already been removed manually.
    """
    if not file_path:
        return
    path = Path(file_path)
    try:
        for sibling in path.parent.glob(f"{glob_escape(path.stem)}.*"):
            sibling.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Failed to delete downloaded file %s", file_path)


def glob_escape(name: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", name)


def _build_match_filter(skip_shorts: bool, shorts_max_seconds: int, skip_live: bool, force: bool, result: dict):
    def match_filter(info_dict, *, incomplete=False):
        if force:
            return None
        live_status = info_dict.get("live_status")
        if skip_live and live_status in ("is_live", "is_upcoming"):
            result["skip_reason"] = f"live stream ({live_status.replace('_', ' ')})"
            return result["skip_reason"]
        if skip_shorts:
            duration = info_dict.get("duration")
            # yt-dlp's own "media_type" (derived from YouTube's isShortsEligible
            # flag) is the authoritative signal; duration/URL is only a fallback
            # for older yt-dlp versions that don't expose it.
            media_type = info_dict.get("media_type")
            webpage_url = info_dict.get("webpage_url") or ""
            is_short = media_type == "short" if media_type is not None else (
                "/shorts/" in webpage_url
                or (duration is not None and duration <= shorts_max_seconds)
            )
            if is_short:
                result["skip_reason"] = (
                    f"short-form video ({duration}s)" if duration is not None else "short-form video"
                )
                return result["skip_reason"]
        return None

    return match_filter


def download_video(
    video_id: str,
    url: str,
    channel_title: str,
    quality: str,
    download_subfolder: str,
    skip_shorts: bool,
    shorts_max_seconds: int,
    skip_live: bool,
    force: bool,
) -> dict:
    """Blocking download. Call via asyncio.to_thread.

    Returns {"path": str | None, "skip_reason": str | None}. When the video is
    filtered out by the Shorts/live match_filter, "path" stays None and
    "skip_reason" explains why - no exception is raised for that case.
    """
    result: dict = {"path": None, "skip_reason": None}

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            percent = round(downloaded / total * 100, 1) if total else None
            progress_state[video_id] = {
                "status": "downloading",
                "percent": percent,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
            }
        elif status == "finished":
            progress_state[video_id] = {"status": "processing", "percent": 100.0}

    def postprocessor_hook(d):
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            filepath = info.get("filepath")
            if filepath:
                result["path"] = filepath

    outdir = config.MEDIA_ROOT / download_subfolder / _sanitize_folder_name(channel_title)
    outtmpl = str(outdir / "%(title)s [%(id)s].%(ext)s")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": config.QUALITY_FORMATS.get(quality, config.QUALITY_FORMATS["best"]),
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "match_filter": _build_match_filter(skip_shorts, shorts_max_seconds, skip_live, force, result),
        **config.youtube_ydl_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    finally:
        progress_state.pop(video_id, None)

    return result


_worker_tasks: list[asyncio.Task] = []
_target_concurrency = 1


async def _worker(worker_id: int) -> None:
    # Checked only between jobs (never mid-download), so shrinking concurrency
    # lets an in-flight download finish and get recorded normally instead of
    # being abandoned half-done.
    while worker_id < _target_concurrency:
        try:
            video = await db.claim_next_pending_video()
            if video is None:
                await asyncio.sleep(3)
                continue

            video_id = video["video_id"]
            channel_title = video["channel_title"] or "Manual"
            url = f"https://www.youtube.com/watch?v={video_id}"
            force = bool(video["force_download"])

            settings = await db.get_settings()

            try:
                result = await asyncio.to_thread(
                    download_video,
                    video_id,
                    url,
                    channel_title,
                    settings.get("quality", "best"),
                    settings.get("download_subfolder", "YouTube"),
                    settings.get("skip_shorts", "true") == "true",
                    int(settings.get("shorts_max_seconds", "180")),
                    settings.get("skip_live", "true") == "true",
                    force,
                )
                if result["skip_reason"]:
                    await db.set_video_status(
                        video_id, "skipped", error=result["skip_reason"], clear_force=True
                    )
                else:
                    await db.set_video_status(
                        video_id, "downloaded", file_path=result["path"], clear_force=True
                    )
            except Exception as exc:  # noqa: BLE001 - want to record any failure and keep looping
                logger.exception("Download failed for %s", video_id)
                await db.set_video_status(video_id, "failed", error=str(exc), clear_force=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the worker alive across unexpected errors
            logger.exception("Worker %s loop iteration failed", worker_id)
            await asyncio.sleep(3)


def _adjust_worker_pool() -> None:
    while len(_worker_tasks) < _target_concurrency:
        _worker_tasks.append(asyncio.create_task(_worker(len(_worker_tasks))))
    if len(_worker_tasks) > _target_concurrency:
        # Don't cancel: excess workers notice worker_id >= _target_concurrency
        # and exit on their own between jobs. Just stop tracking them here.
        del _worker_tasks[_target_concurrency:]


async def start_workers(concurrency: int) -> None:
    global _target_concurrency
    _target_concurrency = max(1, min(concurrency, 5))
    _adjust_worker_pool()


def set_concurrency(concurrency: int) -> None:
    global _target_concurrency
    _target_concurrency = max(1, min(concurrency, 5))
    _adjust_worker_pool()


async def stop_workers() -> None:
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks.clear()
