import asyncio
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, downloader, scheduler, youtube

router = APIRouter(prefix="/api")


@router.post("/channels")
async def add_channel(url: str = Form(...)):
    try:
        channel_id, title, canonical_url = await asyncio.to_thread(youtube.resolve_channel, url)
        await db.add_channel(channel_id, title, canonical_url)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user via redirect for now
        return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: str, enabled: bool = Form(...)):
    await db.set_channel_enabled(channel_id, enabled)
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_id}/toggle-auto-delete")
async def toggle_channel_auto_delete(channel_id: str, enabled: bool = Form(...)):
    await db.set_channel_auto_delete(channel_id, enabled)
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_id}/delete")
async def delete_channel(channel_id: str):
    await db.remove_channel(channel_id)
    return RedirectResponse("/channels", status_code=303)


@router.post("/check-now")
async def check_now():
    await scheduler.poll_all_channels()
    return RedirectResponse("/channels", status_code=303)


@router.post("/videos")
async def add_video(url: str = Form(...)):
    try:
        video_id, title = await asyncio.to_thread(youtube.resolve_video, url)
        # A manually pasted link is an explicit request for that exact video,
        # so it always bypasses the Shorts/live filters.
        existing = await db.get_video(video_id)
        if existing is not None:
            await db.retry_video(video_id, force=True)
        else:
            await db.add_pending_video(video_id, None, title, force=True)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user via redirect for now
        return JSONResponse({"error": str(exc)}, status_code=400)
    return RedirectResponse("/", status_code=303)


@router.post("/videos/{video_id}/retry")
async def retry_video(video_id: str, force: bool = Form(False)):
    await db.retry_video(video_id, force=force)
    return RedirectResponse("/", status_code=303)


@router.post("/videos/{video_id}/delete")
async def delete_video(video_id: str):
    video = await db.get_video(video_id)
    if video is not None:
        await asyncio.to_thread(downloader.delete_downloaded_file, video["file_path"])
        await db.set_video_status(video_id, "deleted")
    return RedirectResponse("/", status_code=303)


@router.post("/videos/check-missing")
async def check_missing_videos(request: Request):
    videos = await db.list_videos_by_status("downloaded")
    for video in videos:
        file_path = video["file_path"]
        exists = bool(file_path) and await asyncio.to_thread(Path(file_path).is_file)
        if not exists:
            await db.set_video_status(video["video_id"], "deleted")
            await db.retry_video(video["video_id"])
    referer = request.headers.get("referer") or "/"
    return RedirectResponse(referer, status_code=303)


@router.post("/settings")
async def update_settings(
    download_subfolder: str = Form(...),
    poll_interval_minutes: int = Form(...),
    quality: str = Form(...),
    auto_delete_days: int = Form(...),
    max_concurrent_downloads: int = Form(...),
    shorts_max_seconds: int = Form(...),
    skip_shorts: str | None = Form(None),
    skip_live: str | None = Form(None),
):
    concurrency = max(1, min(max_concurrent_downloads, 5))
    await db.set_setting("download_subfolder", download_subfolder.strip() or "YouTube")
    await db.set_setting("poll_interval_minutes", str(poll_interval_minutes))
    await db.set_setting("quality", quality)
    await db.set_setting("auto_delete_days", str(max(0, auto_delete_days)))
    await db.set_setting("skip_shorts", "true" if skip_shorts else "false")
    await db.set_setting("shorts_max_seconds", str(max(1, shorts_max_seconds)))
    await db.set_setting("skip_live", "true" if skip_live else "false")
    await db.set_setting("max_concurrent_downloads", str(concurrency))
    scheduler.reschedule(poll_interval_minutes)
    downloader.set_concurrency(concurrency)
    return RedirectResponse("/settings", status_code=303)


@router.get("/status")
async def status():
    videos = await db.list_videos(limit=50)
    return {
        "videos": [
            {
                "video_id": v["video_id"],
                "title": v["title"],
                "channel_title": v["channel_title"],
                "status": v["status"],
                "error": v["error"],
                "progress": downloader.progress_state.get(v["video_id"]),
            }
            for v in videos
        ]
    }
