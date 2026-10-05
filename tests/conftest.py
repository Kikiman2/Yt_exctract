"""Shared fixtures: in-memory DB, fakes wired into the services Context, and a reset of
the module-level state the services keep (worker pool, health cache, pause reasons)."""

import asyncio

import pytest

from app import config, db
from app.services import context, downloads, health
from tests.fakes import FakeLibraryWriter, FakeNotifier, FakeYouTube


@pytest.fixture
async def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "COOKIES_FILE", tmp_path / "data" / "cookies.txt")
    monkeypatch.setattr(config, "JELLYFIN_API_KEY", "")
    monkeypatch.setattr(downloads, "IDLE_POLL", 0.01)
    await db.init_db(":memory:")
    downloads.set_health_reasons([])
    health._cache = None
    health._lock = asyncio.Lock()
    yield db
    await downloads.stop()
    await db.close_db()


@pytest.fixture
def fake_youtube():
    return FakeYouTube()


@pytest.fixture
def fake_writer(tmp_path):
    return FakeLibraryWriter(tmp_path / "library")


@pytest.fixture
def fake_notifier():
    return FakeNotifier()


@pytest.fixture
def services_ctx(database, fake_youtube, fake_writer, fake_notifier):
    ctx = context.Context(youtube=fake_youtube, writer=fake_writer, notifier=fake_notifier)
    context.set_context(ctx)
    yield ctx
