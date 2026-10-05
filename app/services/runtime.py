"""Startup/shutdown of the background machinery. main.py's lifespan calls these after
db.init_db(): they build the services Context (real adapters, or tests.fakes with demo
data when config.DEV_FAKE), check health, start the download pipeline and the scheduler.
Nothing here may crash the app because a dependency is down: the health page shows it."""

from __future__ import annotations

import logging

from .. import config, db
from . import context, downloads, health
from . import settings as settings_service

logger = logging.getLogger(__name__)


def _real_context() -> context.Context:
    # Imported here: yt-dlp is heavy and DEV_FAKE mode never needs it.
    from ..library.jellyfin import JellyfinNotifier
    from ..library.writer import LibraryWriter
    from ..youtube.client import YtDlpClient

    return context.Context(
        youtube=YtDlpClient(),
        writer=LibraryWriter(config.MEDIA_ROOT, config.LIBRARY_SENTINEL),
        notifier=JellyfinNotifier(config.JELLYFIN_URL, config.JELLYFIN_API_KEY,
                                  config.JELLYFIN_LIBRARY_PATH, config.JELLYFIN_LIBRARY_NAME),
    )


async def startup() -> None:
    if config.DEV_FAKE:
        from tests import dev_fake_data

        ctx = dev_fake_data.build_context()
        context.set_context(ctx)
        await dev_fake_data.seed_db(ctx)
    else:
        context.set_context(_real_context())

    # Health first, so the pipeline never starts before we know whether the library is there.
    try:
        await health.check_now()
    except Exception:  # noqa: BLE001
        logger.exception("Initial health check failed")
    await downloads.start()

    from .. import scheduler

    stored = await db.get_settings()
    scheduler.start(settings_service.as_int(stored, "poll_interval_minutes"))
    await db.add_event("info", "startup", "yt_extract started" + (" (fake mode)" if config.DEV_FAKE else ""))


async def shutdown() -> None:
    from .. import scheduler

    scheduler.shutdown()
    await downloads.stop()
    if not context.has_context():
        return
    c = context.ctx()
    try:
        await c.notifier.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("Flushing notifier failed: %s", e)
    try:
        close = getattr(c.notifier, "aclose", None)
        if close is not None:
            await close()
    except Exception as e:  # noqa: BLE001
        logger.warning("Closing notifier failed: %s", e)
