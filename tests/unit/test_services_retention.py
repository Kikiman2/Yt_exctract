"""Retention cleanup: global/per-subscription days, opt-outs, manual videos, offline library."""

import os
import time

from app import config, db
from app.services import retention
from tests.unit.test_services_subscriptions import downloaded

OLD = "2000-01-01T00:00:00"


async def old_download(video_id, ctx, sub=None, when=OLD):
    await downloaded(video_id, ctx, sub, title=video_id)
    await db.update_video(video_id, {"downloaded_at": when})


async def test_expired_video_deleted_and_recent_kept(services_ctx, fake_writer, fake_notifier):
    await db.add_subscription("UC1", "channel", "C", "u")
    await old_download("old00000001", services_ctx, "UC1")
    await downloaded("new00000001", services_ctx, "UC1", title="new00000001")
    assert await retention.cleanup() == 1
    old = await db.get_video("old00000001")
    assert old["status"] == "deleted" and old["file_path"] is None and old["deleted_at"]
    assert (await db.get_video("new00000001"))["status"] == "downloaded"
    assert [p.name for p in fake_writer.root.rglob("*.mp4")] == ["new00000001 [new00000001].mp4"]
    assert fake_notifier.touched == ["YouTube/Chan"]
    assert any(e["kind"] == "retention" for e in await db.list_events())


async def test_global_zero_days_never_deletes(services_ctx):
    await db.set_setting("auto_delete_days", "0")
    await db.add_subscription("UC1", "channel", "C", "u")
    await old_download("old00000001", services_ctx, "UC1")
    assert await retention.cleanup() == 0


async def test_subscription_override_and_optout(services_ctx):
    await db.set_setting("auto_delete_days", "0")
    for sid in ("UC1", "UC2", "UC3"):
        await db.add_subscription(sid, "channel", sid, "u")
        await old_download(f"v{sid}0000000", services_ctx, sid)
    await db.update_subscription("UC1", {"retention_days": 30})           # overrides "never"
    await db.update_subscription("UC3", {"retention_days": 30, "auto_delete_enabled": 0})
    assert await retention.cleanup() == 1
    assert (await db.get_video("vUC10000000"))["status"] == "deleted"
    assert (await db.get_video("vUC20000000"))["status"] == "downloaded"
    assert (await db.get_video("vUC30000000"))["status"] == "downloaded"


async def test_manual_videos_never_expire(services_ctx):
    await old_download("man00000001", services_ctx, None)
    assert await retention.cleanup() == 0


async def test_library_offline_deletes_nothing(services_ctx, fake_writer):
    await db.add_subscription("UC1", "channel", "C", "u")
    await old_download("old00000001", services_ctx, "UC1")
    await old_download("old00000002", services_ctx, "UC1")
    fake_writer.online = False
    assert await retention.cleanup() == 0
    assert (await db.get_video("old00000001"))["status"] == "downloaded"


async def test_one_failing_delete_does_not_stop_others(services_ctx, fake_writer, monkeypatch):
    await db.add_subscription("UC1", "channel", "C", "u")
    await old_download("old00000001", services_ctx, "UC1")
    await old_download("old00000002", services_ctx, "UC1")
    real = fake_writer.delete

    def flaky(path):
        if "old00000001" in path:
            raise OSError("io error")
        real(path)

    monkeypatch.setattr(fake_writer, "delete", flaky)
    assert await retention.cleanup() == 1
    assert (await db.get_video("old00000001"))["status"] == "downloaded"
    assert (await db.get_video("old00000002"))["status"] == "deleted"


async def test_prunes_events_and_stale_staging(services_ctx, monkeypatch):
    monkeypatch.setattr(retention, "MAX_EVENTS", 3)
    for i in range(10):
        await db.add_event("info", "x", f"e{i}")
    stale, fresh = config.STAGING_DIR / "stale", config.STAGING_DIR / "fresh"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))
    await retention.cleanup()
    assert len(await db.list_events(limit=100)) == 3
    assert not stale.exists() and fresh.exists()
