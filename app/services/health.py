"""Health checks and the cached status the dashboard shows. A failing library or
low disk pauses the download pipeline (via downloads.set_health_reasons); every
ok <-> not ok transition is written to the events log."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import httpx

from .. import config, db
from ..youtube import cookies as yt_cookies
from . import downloads
from . import settings as settings_service
from .context import ctx

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 15

_cache: dict | None = None
_lock = asyncio.Lock()

# Components whose failure makes the overall status not ok (cookies and Jellyfin are optional).
_CRITICAL = ("ytdlp", "pot_provider", "library", "disk")


def _empty() -> dict:
    return {
        "ok": False,
        "ytdlp": {"ok": False, "version": None, "detail": "not checked yet"},
        "pot_provider": {"ok": False, "detail": "not checked yet"},
        "cookies": {"present": False, "cookie_count": 0, "youtube_cookie_count": 0,
                    "expires_at": None, "modified_at": None, "detail": "not checked yet"},
        "library": {"ok": False, "path": str(config.MEDIA_ROOT), "detail": "not checked yet"},
        "disk": {"ok": False, "free_gb": None},
        "jellyfin": {"ok": False, "detail": "not checked yet"},
        "checked_at": None,
    }


def _cookies_now() -> dict:
    return dataclasses.asdict(yt_cookies.inspect(config.COOKIES_FILE))


async def status() -> dict:
    """HealthJson. Cached (refreshed by the scheduler), except cookies, which are read
    live so an upload shows up immediately."""
    base = dict(_cache or _empty())
    base["cookies"] = await asyncio.to_thread(_cookies_now)
    return base


def _msg(e: Exception) -> str:
    return str(e) or type(e).__name__


async def _check_ytdlp() -> dict:
    try:
        version = await asyncio.wait_for(asyncio.to_thread(ctx().youtube.version), CHECK_TIMEOUT)
        return {"ok": True, "version": version, "detail": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "version": None, "detail": _msg(e)}


async def _check_pot() -> dict:
    if config.DEV_FAKE:
        return {"ok": True, "detail": "fake mode"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.POT_PROVIDER_URL.rstrip('/')}/ping")
        if r.status_code == 200:
            return {"ok": True, "detail": "ok"}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": _msg(e)}


async def _check_library() -> dict:
    path = str(config.MEDIA_ROOT)
    try:
        writer = ctx().writer
        path = str(writer.root)
        await asyncio.wait_for(asyncio.to_thread(writer.assert_online), CHECK_TIMEOUT)
        return {"ok": True, "path": path, "detail": "ok"}
    except Exception as e:  # noqa: BLE001 - includes a hung network mount (timeout)
        return {"ok": False, "path": path, "detail": _msg(e)}


async def _check_disk(min_gb: int) -> dict:
    free = await downloads.disk_free_gb()
    if free is None:
        return {"ok": False, "free_gb": None}
    return {"ok": free >= min_gb, "free_gb": round(free, 1)}


async def _check_jellyfin() -> dict:
    if not config.JELLYFIN_API_KEY:
        return {"ok": True, "detail": "not configured"}
    try:
        result = await asyncio.wait_for(ctx().notifier.test(), CHECK_TIMEOUT)
        return {"ok": bool(result.get("ok")), "detail": str(result.get("detail", ""))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": _msg(e)}


def _detail(name: str, comp: dict) -> str:
    if name == "disk":
        return f"free space {comp['free_gb']} GB"
    return comp.get("detail", "")


async def _record_transitions(old: dict | None, new: dict) -> None:
    for name in ("ytdlp", "pot_provider", "library", "disk", "jellyfin"):
        was_ok = old[name]["ok"] if old else True  # first check: only failures are news
        now_ok = new[name]["ok"]
        if was_ok and not now_ok:
            await db.add_event("warning", "health", f"{name} is down: {_detail(name, new[name])}")
        elif not was_ok and now_ok:
            await db.add_event("info", "health", f"{name} recovered")


async def check_now() -> dict:
    global _cache
    async with _lock:
        stored = await db.get_settings()
        min_gb = settings_service.as_int(stored, "min_free_gb")
        ytdlp, pot, library, disk, jellyfin, cookies = await asyncio.gather(
            _check_ytdlp(), _check_pot(), _check_library(), _check_disk(min_gb),
            _check_jellyfin(), asyncio.to_thread(_cookies_now))
        result = {
            "ok": False, "ytdlp": ytdlp, "pot_provider": pot, "cookies": cookies,
            "library": library, "disk": disk, "jellyfin": jellyfin, "checked_at": db.now(),
        }
        result["ok"] = all(result[k]["ok"] for k in _CRITICAL)
        await _record_transitions(_cache, result)
        _cache = result
        downloads.set_health_reasons([] if library["ok"] else [downloads.LIBRARY_OFFLINE])
        return dict(result)


async def list_events(limit: int = 100, video_id: str | None = None) -> list[dict]:
    return await db.list_events(limit=limit, video_id=video_id)


async def jellyfin_test() -> dict:
    return await ctx().notifier.test()


async def jellyfin_create_library() -> dict:
    result = await ctx().notifier.create_library()
    await db.add_event("info" if result.get("ok") else "warning", "jellyfin",
                       f"Create library: {result.get('detail', '')}")
    return result


async def upgrade_ytdlp() -> dict:
    version = await asyncio.to_thread(ctx().youtube.upgrade)
    await db.add_event("info", "ytdlp", f"yt-dlp upgraded to {version}")
    return {"version": version, "note": "Restart the container to use the new version."}
