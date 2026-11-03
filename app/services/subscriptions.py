"""Subscriptions (channels / playlists), feed refreshes, and video-level actions
(manual links, retry, delete, missing-file check)."""

from __future__ import annotations

import asyncio
import logging
import posixpath

from .. import config, db
from ..domain import urls
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
    urls.classify_input(value)  # ValueError (400) for input that can't be a channel/playlist
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


async def _queue_manual(ref, user: dict | None = None) -> None:
    existing = await db.get_video(ref.video_id)
    if existing is None:
        await db.add_video(ref.video_id, None, ref.title, force=True, channel_title=ref.channel_title)
    else:
        if user is not None and user["role"] != "admin" and existing["channel_id"] is None:
            # A video nobody holds yet (older manual download, removed subscription) is the
            # admin's: record that first, so this user deleting it later can't remove the file.
            if await db.count_requests(ref.video_id) == 0:
                admin_id = await db.first_admin_id()
                if admin_id is not None:
                    await db.add_request(admin_id, ref.video_id)
        # A user asking for a file that is already in the library just gets it in their
        # list; only the admin's paste of a downloaded link means "download it again".
        already_there = _is_restricted(user) and existing["status"] == "downloaded"
        if existing["status"] != "downloading" and not already_there:
            await db.requeue(ref.video_id, force=True)
    if user is not None:
        await db.add_request(user["id"], ref.video_id)


async def add_playlist_by_url(url: str, user: dict | None = None) -> dict:
    """One-time import: queue every video like a manual link. Each lands in its own
    uploader's channel folder (the playlist itself is not tracked)."""
    playlist = urls.playlist_url(url)
    title, refs = await asyncio.to_thread(ctx().youtube.list_playlist, playlist)
    if not refs:
        raise ValueError("That playlist has no downloadable videos")
    for ref in refs:
        await _queue_manual(ref, user)
    await db.add_event("info", "manual", f"Queued {len(refs)} video(s) from playlist {title}")
    return {"playlist": title, "queued": len(refs)}


async def add_video_by_url(url: str, user: dict | None = None) -> dict:
    value = (url or "").strip()
    if not value:
        raise ValueError("Enter a video URL")
    if urls.playlist_url(value):
        return await add_playlist_by_url(value, user)
    ref = await asyncio.to_thread(ctx().youtube.resolve_video, value)
    await _queue_manual(ref, user)
    await db.add_event("info", "manual", f"Queued {ref.title}", ref.video_id)
    return await _video_json_by_id(ref.video_id)


async def list_videos(status: str | None = None, source_id: str | None = None,
                      limit: int = 200, offset: int = 0, user: dict | None = None) -> list[dict]:
    """Admins (and user=None) see every video; other users only the ones they requested."""
    if status and status not in VIDEO_STATUSES:
        raise ValueError(f"unknown status: {status}")
    if user is not None and user["role"] != "admin":
        rows = await db.list_videos_for_user(user["id"], limit=limit, offset=offset,
                                             status=status or None)
    else:
        rows = await db.list_videos(limit=limit, offset=offset, status=status or None,
                                    subscription_id=source_id or None)
    return [video_json(r) for r in rows]


def _is_restricted(user: dict | None) -> bool:
    return user is not None and user["role"] != "admin"


async def _row_for(video_id: str, user: dict | None) -> dict:
    """The video row; for a non-admin user, 404 unless they requested it."""
    row = await db.get_video(video_id)
    if row is None or (_is_restricted(user) and not await db.has_request(user["id"], video_id)):
        raise KeyError(video_id)
    return row


async def retry_video(video_id: str, force: bool = False, user: dict | None = None) -> dict:
    row = await _row_for(video_id, user)
    if row["status"] == "downloading":
        raise ValueError("This video is downloading right now")
    await db.requeue(video_id, force=force)
    await db.add_event("info", "retry", "Queued again" + (" (forced)" if force else ""), video_id)
    return await _video_json_by_id(video_id)


async def delete_video(video_id: str, user: dict | None = None) -> dict:
    """Admin: deletes the file. Other user: drops the video from their own list, and
    deletes the file only when nobody else holds it (no other requester, no subscription)."""
    row = await _row_for(video_id, user)
    if row["status"] == "downloading":
        raise ValueError("This video is downloading right now")
    if _is_restricted(user):
        await db.remove_request(user["id"], video_id)
        if row["channel_id"] is not None or await db.count_requests(video_id) > 0:
            await db.add_event("info", "removed", f"Removed from {user['username']}'s list", video_id)
            return video_json(row)
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
