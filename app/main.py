"""FastAPI app factory: middleware order, routers, lifespan.

The lifespan refuses to start without credentials (auth.check_config), opens the
database, then lets services.runtime start the pipeline and scheduler. Shutdown
flushes the Jellyfin notifier (runtime.shutdown) before the database closes."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db
from .routes import api, pages
from .services import runtime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.check_config()
    await db.init_db()
    try:
        await runtime.startup()
        try:
            yield
        finally:
            try:
                await runtime.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must still close the db
                log.exception("runtime shutdown failed")
    finally:
        await db.close_db()


async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Without this, browsers fall back to heuristic caching and can keep
        # serving a stale app.js/style.css after a deploy until a hard refresh.
        response.headers["Cache-Control"] = "no-cache"
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="yt_extract", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Innermost first: CSRF check, static no-cache, then the session outermost.
    app.middleware("http")(auth.csrf_middleware)
    app.middleware("http")(no_cache_static)
    app.add_middleware(SessionMiddleware, **auth.session_middleware_kwargs())

    api.install_error_handlers(app)
    app.add_exception_handler(auth.NotAuthenticated, auth.not_authenticated_handler)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(pages.public_router)
    app.include_router(pages.router)
    app.include_router(api.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return app


app = create_app()
