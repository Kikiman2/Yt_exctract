"""JSON API used by the pages' JavaScript. Every route requires a session.

Errors from the services layer are mapped to JSON {"detail": "..."}:
KeyError -> 404, ValueError -> 400, YouTubeError -> 502, LibraryOffline -> 503."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth, models
from ..services import cookies, downloads, health, settings, subscriptions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_login)])

MAX_COOKIES_BYTES = 1024 * 1024
BOT_HINT = " (YouTube wants a login: upload fresh cookies under Settings)"


# --- error mapping ------------------------------------------------------------


def _detail(exc: BaseException, fallback: str) -> str:
    if isinstance(exc, KeyError) and exc.args:
        arg = exc.args[0]
        return str(arg) if isinstance(arg, str) and " " in arg else f"Not found: {arg}"
    text = str(exc).strip()
    return text or fallback


def install_error_handlers(app: FastAPI) -> None:
    async def key_error(request: Request, exc: KeyError):
        return JSONResponse({"detail": _detail(exc, "Not found")}, status_code=404)

    async def value_error(request: Request, exc: ValueError):
        return JSONResponse({"detail": _detail(exc, "Invalid request")}, status_code=400)

    async def youtube_error(request: Request, exc: models.YouTubeError):
        text = _detail(exc, "unknown error")
        if exc.bot_check:
            text += BOT_HINT
        return JSONResponse({"detail": f"YouTube failed: {text}"}, status_code=502)

    async def library_offline(request: Request, exc: models.LibraryOffline):
        return JSONResponse(
            {"detail": f"Library is offline: {_detail(exc, 'sentinel file missing')}"}, status_code=503)

    app.add_exception_handler(KeyError, key_error)
    app.add_exception_handler(ValueError, value_error)
    app.add_exception_handler(models.YouTubeError, youtube_error)
    app.add_exception_handler(models.LibraryOffline, library_offline)


# --- request bodies -----------------------------------------------------------


class UrlIn(BaseModel):
    url: str


class RetryIn(BaseModel):
    force: bool = False


# --- status / queue -----------------------------------------------------------


@router.get("/status")
async def status():
    return {"health": await health.status(), "queue": await downloads.queue_status()}


@router.post("/queue/pause")
async def queue_pause():
    await downloads.pause()
    return await downloads.queue_status()


@router.post("/queue/resume")
async def queue_resume():
    await downloads.resume()
    return await downloads.queue_status()


# --- videos -------------------------------------------------------------------


@router.get("/videos")
async def list_videos(
    status: str | None = None,
    source_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return await subscriptions.list_videos(status, source_id, limit, offset)


@router.post("/videos")
async def add_video(body: UrlIn):
    return await subscriptions.add_video_by_url(body.url)


# Registered before the {video_id} routes so "check-missing" is never read as an id.
@router.post("/videos/check-missing")
async def check_missing():
    return {"requeued": await subscriptions.check_missing()}


@router.post("/videos/{video_id}/retry")
async def retry_video(video_id: str, body: RetryIn | None = None):
    return await subscriptions.retry_video(video_id, force=bool(body and body.force))


@router.post("/videos/{video_id}/delete")
async def delete_video(video_id: str):
    return await subscriptions.delete_video(video_id)


# --- sources ------------------------------------------------------------------


@router.get("/sources")
async def list_sources():
    return await subscriptions.list_sources()


@router.post("/sources")
async def add_source(body: UrlIn):
    return await subscriptions.add_source(body.url)


@router.patch("/sources/{source_id}")
async def update_source(source_id: str, fields: dict[str, Any]):
    return await subscriptions.update_source(source_id, fields)


@router.delete("/sources/{source_id}")
async def remove_source(source_id: str):
    await subscriptions.remove_source(source_id)
    return {"ok": True}


@router.post("/sources/{source_id}/refresh")
async def refresh_source(source_id: str):
    return {"new": await subscriptions.refresh_one(source_id)}


@router.post("/refresh")
async def refresh_all():
    return {"new": await subscriptions.refresh_all()}


# --- settings -----------------------------------------------------------------


@router.get("/settings")
async def get_settings():
    return await settings.get_settings()


@router.put("/settings")
async def put_settings(values: dict[str, Any]):
    # The service validates every key (and names it in the error) before storing any.
    return await settings.update_settings(values)


# --- health / events / integrations ---------------------------------------------


@router.get("/health")
async def get_health():
    return await health.status()


@router.post("/health/check")
async def check_health():
    return await health.check_now()


@router.get("/events")
async def events(limit: int = Query(100, ge=1, le=1000), video_id: str | None = None):
    return await health.list_events(limit=limit, video_id=video_id)


@router.post("/jellyfin/test")
async def jellyfin_test():
    return await health.jellyfin_test()


@router.post("/jellyfin/create-library")
async def jellyfin_create_library():
    return await health.jellyfin_create_library()


@router.post("/ytdlp/upgrade")
async def ytdlp_upgrade():
    return await health.upgrade_ytdlp()


# --- cookies ------------------------------------------------------------------


@router.get("/cookies")
async def get_cookies():
    return await cookies.status()


@router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    # Read one byte over the cap so an oversized upload is detected without buffering it all.
    data = await file.read(MAX_COOKIES_BYTES + 1)
    if len(data) > MAX_COOKIES_BYTES:
        raise ValueError("Cookies file is too large (limit 1 MB)")
    return await cookies.save(data)


@router.delete("/cookies")
async def delete_cookies():
    return await cookies.delete()
