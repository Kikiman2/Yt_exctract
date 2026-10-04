import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, downloader, scheduler
from .routes import api, pages

logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_startup_check_task: asyncio.Task | None = None
_startup_cleanup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_check_task, _startup_cleanup_task
    await db.init_db()
    settings = await db.get_settings()
    scheduler.start(int(settings.get("poll_interval_minutes", "15")))
    await downloader.start_workers(int(settings.get("max_concurrent_downloads", "2")))
    _startup_check_task = asyncio.create_task(scheduler.poll_all_channels())
    _startup_cleanup_task = asyncio.create_task(scheduler.cleanup_old_downloads())
    yield
    await downloader.stop_workers()
    scheduler.scheduler.shutdown(wait=False)
    await db.close_db()


app = FastAPI(title="YT Extract", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Without this, browsers fall back to heuristic caching and can keep
        # serving a stale app.js/style.css after a deploy until a hard refresh.
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(pages.router)
app.include_router(api.router)
