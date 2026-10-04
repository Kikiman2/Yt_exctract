"""Subscriptions (channels / playlists), feed refreshes, and video-level actions
(manual links, retry, delete, missing-file check)."""

from __future__ import annotations

import asyncio
import logging
import posixpath

from .. import config, db
from ..models import SourceRef, VIDEO_STATUSES
from .context import ctx
from .downloads import video_json

logger = logging.getLogger(__name__)


def _opt_bool(row_value) -> bool | None:
    return None if row_value is None else bool(row_value)


def _sub_json(row: dict) -> dict:
    return {
        "id": row["channel_id"],
        "kind": row["kind"],
        "title": row["title"],
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        "auto_delete_enabled": bool(row["auto_delete_enabled"]),
        "quality": row["quality"],
        "skip_shorts": _opt_bool(row["skip_shorts"]),
        "skip_live": _opt_bool(row["skip_live"]),
        "retention_days": row["retention_days"],
        "last_checked_at": row["last_checked_at"],
        "last_error": row["last_error"],
        "video_count": row.get("video_count", 0),
        "downloaded_count": row.get("downloaded_count", 0),
    }


async def _sub_json_by_id(source_id: str) -> dict:
    for row in await db.list_subscriptions():
        if row["channel_id"] == source_id:
            return _sub_json(row)
    raise KeyError(source_id)


async def list_sources() -> list[dict]:
    return [_sub_json(r) for r in await db.list_subscriptions()]


async def add_source(url: str) -> dict:
    value = (url or "").strip()
    if not value:
        raise ValueError("Enter a channel or playlist URL or @handle")
    ref = await asyncio.to_thread(ctx().youtube.resolve_source, value)
    if not await db.add_subscription(ref.source_id, ref.kind, ref.title, ref.url):
        raise ValueError(f"Already subscribed to {ref.title}")
    await db.add_event("info", "subscribed", f"Subscribed to {ref.kind} {ref.title}")
    try:
        await refresh_one(ref.source_id)
    except Exception:  # noqa: BLE001 - recorded on the subscription; adding itself succeeded
        logger.info("First refresh of %s failed", ref.source_id)
    return await _sub_json_by_id(ref.source_id)


def _check_override(key: str, value):
    if key in ("enabled", "auto_delete_enabled"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return int(value)
    if key in ("skip_shorts", "skip_live"):
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{key} must be true, false or null")
        return None if value is None else int(value)
    if key == "quality":
        if value is not None and value not in config.QUALITY_FORMATS:
            raise ValueError(f"quality must be null or one of: {', '.join(config.QUALITY_FORMATS)}")
        return value
    if key == "retention_days":
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or not 0 <= value <= 3650):
            raise ValueError("retention_days must be null or a whole number between 0 and 3650")
        return value
    raise ValueError(f"unknown field: {key}")


async def update_source(source_id: str, fields: dict) -> dict:
    if await db.get_subscription(source_id) is None:
        raise KeyError(source_id)
    clean = {k: _check_override(k, v) for k, v in fields.items()}
    await db.update_subscription(source_id, clean)
    return await _sub_json_by_id(source_id)


async def remove_source(source_id: str) -> None:
    sub = await db.get_subscription(source_id)
    if sub is None:
        raise KeyError(source_id)
    await db.remove_subscription(source_id)
    await db.add_event("info", "unsubscribed", f"Unsubscribed from {sub['title']}")


async def refresh_one(source_id: str) -> int:
    """Queue the feed's unknown videos. On failure the error is stored on the
    subscription (and logged once per distinct message) and then re-raised."""
    sub = await db.get_subscription(source_id)
    if sub is None:
        raise KeyError(source_id)
    ref = SourceRef(sub["kind"], source_id, sub["title"], sub["url"])
    try:
        entries = await asyncio.to_thread(ctx().youtube.fetch_feed, ref)
    except Exception as e:
        message = str(e) or type(e).__name__
        await db.update_subscription(source_id, {"last_checked_at": db.now(), "last_error": message})
        if sub["last_error"] != message:
            await db.add_event("warning", "refresh_failed", f"{sub['title']}: {message}")
        raise
    new = 0
    seen: set[str] = set()
    for entry in entries:
        if entry.video_id in seen:
            continue
        seen.add(entry.video_id)
        if await db.add_video(entry.video_id, source_id, entry.title, channel_title=sub["title"]):
            new += 1
    await db.update_subscription(source_id, {"last_checked_at": db.now(), "last_error": None})
    if new:
        await db.add_event("info", "refresh", f"{sub['title']}: {new} new video(s) queued")
    return new


async def refresh_all() -> int:
    """Scheduler job. One source failing never stops the rest."""
    total = 0
    for sub in await db.list_enabled_subscriptions():
        try:
            total += await refresh_one(sub["channel_id"])
        except Exception:  # noqa: BLE001 - already recorded by refresh_one
            continue
    return total


# --- videos -----------------------------------------------------------------------


async def _video_json_by_id(video_id: str) -> dict:
    row = await db.get_video(video_id)
    if row is None:
        raise KeyError(video_id)
    return video_json(row)


async def add_video_by_url(url: str) -> dict:
    value = (url or "").strip()
    if not value:
        raise ValueError("Enter a video URL")
    ref = await asyncio.to_thread(ctx().youtube.resolve_video, value)
    existing = await db.get_video(ref.video_id)
    if existing is None:
        await db.add_video(ref.video_id, None, ref.title, force=True, channel_title=ref.channel_title)
    elif existing["status"] != "downloading":
        await db.requeue(ref.video_id, force=True)
    await db.add_event("info", "manual", f"Queued {ref.title}", ref.video_id)
    return await _video_json_by_id(ref.video_id)


async def list_videos(status: str | None = None, source_id: str | None = None,
                      limit: int = 200, offset: int = 0) -> list[dict]:
    if status and status not in VIDEO_STATUSES:
        raise ValueError(f"unknown status: {status}")
    rows = await db.list_videos(limit=limit, offset=offset, status=status or None,
                                subscription_id=source_id or None)
    return [video_json(r) for r in rows]


async def retry_video(video_id: str, force: bool = False) -> dict:
    row = await db.get_video(video_id)
    if row is None:
        raise KeyError(video_id)
    if row["status"] == "downloading":
        raise ValueError("This video is downloading right now")
    await db.requeue(video_id, force=force)
    await db.add_event("info", "retry", "Queued again" + (" (forced)" if force else ""), video_id)
    return await _video_json_by_id(video_id)


async def delete_video(video_id: str) -> dict:
    row = await db.get_video(video_id)
    if row is None:
        raise KeyError(video_id)
    if row["status"] == "downloading":
        raise ValueError("This video is downloading right now")
    c = ctx()
    if row["file_path"]:
        await asyncio.to_thread(c.writer.delete, row["file_path"])  # LibraryOffline -> 503
        c.notifier.touch(posixpath.dirname(row["file_path"]))
    await db.update_video(video_id, {
        "status": "deleted", "deleted_at": db.now(), "file_path": None, "file_size": None,
        "error": None, "next_retry_at": None,
    })
    await db.add_event("info", "deleted", "Deleted by user", video_id)
    return await _video_json_by_id(video_id)


async def check_missing() -> int:
    """Downloaded rows whose file vanished go back to pending. Refuses to run while the
    library is offline, when every file would look missing."""
    c = ctx()
    await asyncio.to_thread(c.writer.assert_online)
    n = 0
    for row in await db.list_downloaded():
        path = row["file_path"]
        if path and await asyncio.to_thread(c.writer.exists, path):
            continue
        await db.requeue(row["video_id"])
        await db.update_video(row["video_id"], {"file_path": None, "file_size": None})
        await db.add_event("warning", "missing", "File missing from the library, queued again",
                           row["video_id"])
        n += 1
    return n
