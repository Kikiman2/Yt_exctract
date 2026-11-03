"""JSON API used by the pages' JavaScript. Every route requires a session; `router` is open
to any signed-in account, `admin_router` (sources, settings, queue control, health,
cookies, users) needs role=admin and answers 403 otherwise.

Errors from the services layer are mapped to JSON {"detail": "..."}:
KeyError -> 404, ValueError -> 400, YouTubeError -> 502, LibraryOffline -> 503."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth, models
from ..services import cookies, downloads, health, settings, subscriptions, users

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_login)])
admin_router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_admin)])

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


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class NewUserIn(BaseModel):
    username: str
    password: str
    role: str = "user"


# --- status / queue -----------------------------------------------------------


@router.get("/status")
async def status(user: dict = Depends(auth.require_login)):
    if user["role"] != "admin":
        # Library paths, Jellyfin and disk details stay admin-only; users only learn
        # whether downloads are paused (the banner on every page).
        reasons = await downloads.pause_reasons()
        return {"queue": {"paused": bool(reasons), "pause_reasons": reasons}}
    return {"health": await health.status(), "queue": await downloads.queue_status()}


@admin_router.post("/queue/pause")
async def queue_pause():
    await downloads.pause()
    return await downloads.queue_status()


@admin_router.post("/queue/resume")
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
    user: dict = Depends(auth.require_login),
):
    return await subscriptions.list_videos(status, source_id, limit, offset, user=user)


@router.post("/videos")
async def add_video(body: UrlIn, user: dict = Depends(auth.require_login)):
    return await subscriptions.add_video_by_url(body.url, user)


# Registered before the {video_id} routes so "check-missing" is never read as an id.
@admin_router.post("/videos/check-missing")
async def check_missing():
    return {"requeued": await subscriptions.check_missing()}


@router.post("/videos/{video_id}/retry")
async def retry_video(video_id: str, body: RetryIn | None = None,
                      user: dict = Depends(auth.require_login)):
    return await subscriptions.retry_video(video_id, force=bool(body and body.force), user=user)


@router.post("/videos/{video_id}/delete")
async def delete_video(video_id: str, user: dict = Depends(auth.require_login)):
    return await subscriptions.delete_video(video_id, user=user)


# --- sources ------------------------------------------------------------------


@admin_router.get("/sources")
async def list_sources():
    return await subscriptions.list_sources()


@admin_router.post("/sources")
async def add_source(body: UrlIn):
    return await subscriptions.add_source(body.url)


@admin_router.patch("/sources/{source_id}")
async def update_source(source_id: str, fields: dict[str, Any]):
    return await subscriptions.update_source(source_id, fields)


@admin_router.delete("/sources/{source_id}")
async def remove_source(source_id: str):
    await subscriptions.remove_source(source_id)
    return {"ok": True}


@admin_router.post("/sources/{source_id}/refresh")
async def refresh_source(source_id: str):
    return {"new": await subscriptions.refresh_one(source_id)}


@admin_router.post("/refresh")
async def refresh_all():
    return {"new": await subscriptions.refresh_all()}


# --- settings -----------------------------------------------------------------


@admin_router.get("/settings")
async def get_settings():
    return await settings.get_settings()


@admin_router.put("/settings")
async def put_settings(values: dict[str, Any]):
    # The service validates every key (and names it in the error) before storing any.
    return await settings.update_settings(values)


# --- health / events / integrations ---------------------------------------------


@admin_router.get("/health")
async def get_health():
    return await health.status()


@admin_router.post("/health/check")
async def check_health():
    return await health.check_now()


@admin_router.get("/events")
async def events(limit: int = Query(100, ge=1, le=1000), video_id: str | None = None):
    return await health.list_events(limit=limit, video_id=video_id)


@admin_router.post("/jellyfin/test")
async def jellyfin_test():
    return await health.jellyfin_test()


@admin_router.post("/jellyfin/create-library")
async def jellyfin_create_library():
    return await health.jellyfin_create_library()


@admin_router.post("/ytdlp/upgrade")
async def ytdlp_upgrade():
    return await health.upgrade_ytdlp()


# --- cookies ------------------------------------------------------------------


@admin_router.get("/cookies")
async def get_cookies():
    return await cookies.status()


@admin_router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    # Read one byte over the cap so an oversized upload is detected without buffering it all.
    data = await file.read(MAX_COOKIES_BYTES + 1)
    if len(data) > MAX_COOKIES_BYTES:
        raise ValueError("Cookies file is too large (limit 1 MB)")
    return await cookies.save(data)


@admin_router.delete("/cookies")
async def delete_cookies():
    return await cookies.delete()


# --- account (any signed-in user) ----------------------------------------------


@router.get("/me")
async def me(user: dict = Depends(auth.require_login)):
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@router.post("/me/password")
async def change_password(body: PasswordIn, user: dict = Depends(auth.require_login)):
    await users.change_own_password(user, body.current_password, body.new_password)
    return {"ok": True}


# --- users (admin) --------------------------------------------------------------


@admin_router.get("/users")
async def list_users():
    return await users.list_users()


@admin_router.post("/users")
async def create_user(body: NewUserIn):
    return await users.create_user(body.username, body.password, body.role)


@admin_router.patch("/users/{user_id}")
async def update_user(user_id: int, fields: dict[str, Any], actor: dict = Depends(auth.require_admin)):
    return await users.update_user(actor, user_id, fields)


@admin_router.delete("/users/{user_id}")
async def delete_user(user_id: int, actor: dict = Depends(auth.require_admin)):
    await users.delete_user(actor, user_id)
    return {"ok": True}
