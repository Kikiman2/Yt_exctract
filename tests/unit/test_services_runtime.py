"""runtime.startup/shutdown: DEV_FAKE wiring with demo data, and tolerance of broken adapters."""

import pytest

from app import config, db, scheduler
from app.services import context, downloads, health, runtime, subscriptions
from app.models import VIDEO_STATUSES


@pytest.fixture
async def fake_mode(database, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    yield
    await runtime.shutdown()


async def test_dev_fake_startup_seeds_demo_data(fake_mode):
    await runtime.startup()
    subs = await subscriptions.list_sources()
    assert {s["kind"] for s in subs} == {"channel", "playlist"} and len(subs) >= 3
    counts = await db.count_by_status()
    # every status is represented in the seed (pending ones may be picked up by workers)
    assert set(counts) | {"pending", "downloading", "downloaded"} == set(VIDEO_STATUSES)
    assert (await health.status())["ok"] is True
    assert scheduler.scheduler.running
    assert {j.id for j in scheduler.scheduler.get_jobs()} >= {
        "refresh_all", "retry_due", "cleanup", "health"}
    kinds = {e["kind"] for e in await db.list_events()}
    assert {"startup", "subscribed", "download_failed"} <= kinds
    # seeded downloads have real files in the fake library
    for row in await db.list_downloaded():
        if row["video_id"].startswith("demo_"):
            assert context.ctx().writer.exists(row["file_path"])


async def test_dev_fake_startup_is_idempotent(fake_mode):
    await runtime.startup()
    n = len(await subscriptions.list_sources())
    await runtime.shutdown()
    await runtime.startup()
    assert len(await subscriptions.list_sources()) == n


async def test_dev_fake_pipeline_downloads_pending_demo_videos(fake_mode):
    import asyncio

    await runtime.startup()
    for _ in range(200):
        if (await db.get_video("demo_pend_1"))["status"] == "downloaded":
            break
        await asyncio.sleep(0.02)
    row = await db.get_video("demo_pend_1")
    assert row["status"] == "downloaded" and context.ctx().writer.exists(row["file_path"])


async def test_startup_survives_everything_being_down(database, tmp_path, monkeypatch):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("down")

    monkeypatch.setattr(config, "DEV_FAKE", False)
    monkeypatch.setattr(runtime, "_real_context",
                        lambda: context.Context(youtube=Broken(), writer=Broken(), notifier=Broken()))
    await runtime.startup()
    h = await health.status()
    assert h["ok"] is False and h["ytdlp"]["ok"] is False and h["library"]["ok"] is False
    assert downloads.LIBRARY_OFFLINE in await downloads.pause_reasons()
    await runtime.shutdown()  # flush on a broken notifier must not raise


async def test_scheduler_reschedule_while_running(fake_mode):
    await runtime.startup()
    scheduler.reschedule_poll(7)
    job = scheduler.scheduler.get_job("refresh_all")
    assert job.trigger.interval.total_seconds() == 7 * 60
