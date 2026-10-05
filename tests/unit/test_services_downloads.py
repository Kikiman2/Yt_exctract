"""Download state machine, retry/backoff, pause reasons, overrides, worker pool."""

import asyncio
import datetime

import pytest

from app import config, db
from app.models import YouTubeError
from app.services import downloads, subscriptions
from app.services.downloads import process_one


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def queue(video_id="vid00000001", channel=None, title="Some video", force=False):
    sub_id = None
    if channel:
        await db.add_subscription(channel, "channel", f"Chan {channel}", "https://y/c")
        sub_id = channel
    await db.add_video(video_id, sub_id, title, force=force, channel_title=f"Chan {channel}" if channel else None)


async def claim_and_run():
    video = await db.claim_next()
    assert video is not None
    await process_one(video)
    return await db.get_video(video["video_id"])


async def test_success_installs_sidecars_touches_and_cleans_staging(services_ctx, fake_writer, fake_notifier):
    await queue(channel="UC1")
    row = await claim_and_run()
    assert row["status"] == "downloaded" and row["error"] is None
    assert row["file_path"].startswith("YouTube/Chan UC1/")
    assert row["file_path"].endswith(" [vid00000001].mp4")
    assert fake_writer.exists(row["file_path"])
    assert row["file_size"] == len(b"fake-video-vid00000001")
    assert row["duration"] == 600 and row["upload_date"] == "2024-05-06"
    folder = fake_writer.root / "YouTube" / "Chan UC1"
    assert sorted(p.suffix for p in folder.iterdir()) == [".jpg", ".mp4", ".nfo"]
    assert fake_notifier.touched == ["YouTube/Chan UC1"]
    assert not (config.STAGING_DIR / "vid00000001").exists()
    assert any(e["kind"] == "downloaded" for e in await db.list_events())


async def test_write_nfo_off_skips_sidecars(services_ctx, fake_writer):
    await db.set_setting("write_nfo", "false")
    await queue(channel="UC1")
    row = await claim_and_run()
    files = [p.name for p in (fake_writer.root / "YouTube" / "Chan UC1").iterdir()]
    assert row["status"] == "downloaded" and len(files) == 1


async def test_manual_video_uses_snapshot_channel_and_force(services_ctx, fake_youtube):
    await db.add_video("manual00001", None, "T", force=True, channel_title="Someone")
    row = await claim_and_run()
    assert row["file_path"].startswith("YouTube/Someone/")
    assert fake_youtube.downloads[0].force is True


async def test_manual_video_without_channel_goes_to_meta_channel(services_ctx):
    await queue()
    row = await claim_and_run()
    assert "/Fake Channel/" in row["file_path"]


async def test_skip_keeps_reason_in_error(services_ctx, fake_youtube, fake_writer):
    fake_youtube.durations["vid00000001"] = 30
    await queue(channel="UC1")
    row = await claim_and_run()
    assert row["status"] == "skipped" and "short-form" in row["error"]
    assert row["attempts"] == 0
    assert not any(fake_writer.root.rglob("*.mp4"))
    assert not (config.STAGING_DIR / "vid00000001").exists()


async def test_failure_backoff_and_event(services_ctx, fake_youtube):
    fake_youtube.fail["vid00000001"] = YouTubeError("Video unavailable")
    await queue()
    row = await claim_and_run()
    assert row["status"] == "failed" and row["attempts"] == 1 and row["error"] == "Video unavailable"
    delta = datetime.datetime.fromisoformat(row["next_retry_at"]) - datetime.datetime.fromisoformat(db.now())
    assert datetime.timedelta(hours=5, minutes=55) < delta < datetime.timedelta(hours=6, minutes=5)
    assert not (config.STAGING_DIR / "vid00000001").exists()
    events = await db.list_events(video_id="vid00000001")
    assert events[0]["level"] == "error" and "attempt 1/5" in events[0]["message"]


async def test_backoff_doubles_and_gives_up(services_ctx, fake_youtube):
    fake_youtube.fail["vid00000001"] = YouTubeError("boom")
    await queue()
    await db.set_setting("max_attempts", "3")
    await db.update_video("vid00000001", {"attempts": 1})
    row = await claim_and_run()
    delta = datetime.datetime.fromisoformat(row["next_retry_at"]) - datetime.datetime.fromisoformat(db.now())
    assert delta > datetime.timedelta(hours=11)  # 6h * 2
    await db.requeue("vid00000001", reset_attempts=False)
    row = await claim_and_run()
    assert row["attempts"] == 3 and row["next_retry_at"] is None  # out of attempts


async def test_bot_check_fails_with_hint_and_does_not_pause(services_ctx, fake_youtube):
    fake_youtube.fail["vid00000001"] = YouTubeError("Sign in to confirm you're not a bot", bot_check=True)
    await queue()
    row = await claim_and_run()
    assert row["status"] == "failed" and "cookies" in row["error"]
    assert await downloads.pause_reasons() == []


async def test_unexpected_exception_fails_video(services_ctx, fake_youtube, monkeypatch):
    def boom(req, cb=None):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(fake_youtube, "download", boom)
    await queue()
    row = await claim_and_run()
    assert row["status"] == "failed" and "disk exploded" in row["error"]


async def test_library_offline_returns_to_pending_without_attempt(services_ctx, fake_writer):
    fake_writer.online = False
    await queue()
    row = await claim_and_run()
    assert row["status"] == "pending" and row["attempts"] == 0 and row["started_at"] is None
    assert downloads.LIBRARY_OFFLINE in await downloads.pause_reasons()
    assert await db.claim_next() is not None  # still claimable once resumed
    assert not (config.STAGING_DIR / "vid00000001").exists()


async def test_library_offline_during_install(services_ctx, fake_writer, fake_youtube, monkeypatch):
    orig = fake_youtube.download

    def download_then_unmount(req, cb=None):
        result = orig(req, cb)
        fake_writer.online = False  # share vanished mid-download
        return result

    monkeypatch.setattr(fake_youtube, "download", download_then_unmount)
    await queue()
    row = await claim_and_run()
    assert row["status"] == "pending" and row["attempts"] == 0
    assert downloads.LIBRARY_OFFLINE in await downloads.pause_reasons()
    assert not (config.STAGING_DIR / "vid00000001").exists()


async def test_overrides_resolve_over_globals(services_ctx, fake_youtube):
    await queue("vid00000001", channel="UC1")
    await db.update_subscription("UC1", {"quality": "720p", "skip_shorts": 0, "skip_live": 0})
    fake_youtube.durations["vid00000001"] = 30  # short, but this subscription allows shorts
    row = await claim_and_run()
    req = fake_youtube.downloads[0]
    assert req.quality == "720p" and req.skip_shorts is False and req.skip_live is False
    assert row["status"] == "downloaded"
    await queue("vid00000002")  # no subscription: globals
    await claim_and_run()
    req2 = fake_youtube.downloads[1]
    assert req2.quality == "best" and req2.skip_shorts is True and req2.skip_live is True


async def test_global_quality_setting_used(services_ctx, fake_youtube):
    await db.set_setting("quality", "1080p")
    await queue()
    await claim_and_run()
    assert fake_youtube.downloads[0].quality == "1080p"


async def test_download_subfolder_setting(services_ctx):
    await db.set_setting("download_subfolder", "Vids")
    await queue(channel="UC1")
    row = await claim_and_run()
    assert row["file_path"].startswith("Vids/")


async def test_progress_visible_while_downloading(services_ctx, fake_youtube):
    fake_youtube.delay.clear()
    await queue()
    video = await db.claim_next()
    task = asyncio.create_task(process_one(video))
    await wait_for(lambda: _has_progress("vid00000001"))
    assert downloads.get_progress("vid00000001")["status"] == "downloading"
    status = await downloads.queue_status()
    assert status["active"][0]["video_id"] == "vid00000001"
    assert status["active"][0]["progress"]["status"] == "downloading"
    fake_youtube.delay.set()
    await task
    assert downloads.get_progress("vid00000001") is None


async def _has_progress(video_id):
    return downloads.get_progress(video_id) is not None


async def test_retry_due_requeues_only_due_with_attempts_left(services_ctx):
    for vid in ("a0000000001", "b0000000002", "c0000000003"):
        await queue(vid)
    past, future = "2000-01-01T00:00:00", "2999-01-01T00:00:00"
    await db.update_video("a0000000001", {"status": "failed", "attempts": 1, "next_retry_at": past})
    await db.update_video("b0000000002", {"status": "failed", "attempts": 1, "next_retry_at": future})
    await db.update_video("c0000000003", {"status": "failed", "attempts": 5, "next_retry_at": past})
    assert await downloads.retry_due() == 1
    assert (await db.get_video("a0000000001"))["status"] == "pending"


async def test_pause_reasons_user_health_and_low_disk(services_ctx, monkeypatch):
    assert await downloads.pause_reasons() == []
    await downloads.pause()
    assert await downloads.pause_reasons() == [downloads.USER_PAUSED]
    await downloads.resume()
    downloads.set_health_reasons(["Library offline"])
    assert await downloads.pause_reasons() == ["Library offline"]
    downloads.set_health_reasons([])

    async def fake_free():
        return 1.0

    monkeypatch.setattr(downloads, "disk_free_gb", fake_free)
    (reason,) = await downloads.pause_reasons()
    assert "Low disk space" in reason
    await db.set_setting("min_free_gb", "0")  # 0 disables the check
    assert await downloads.pause_reasons() == []


async def test_queue_status_shape(services_ctx):
    await queue("a0000000001")
    await downloads.pause()
    q = await downloads.queue_status()
    assert q["paused"] is True and q["pause_reasons"] == [downloads.USER_PAUSED]
    assert q["counts"]["pending"] == 1 and set(q["counts"]) >= {"downloaded", "failed", "deleted"}
    assert q["active"] == []


async def test_workers_download_queued_videos(services_ctx):
    for i in range(3):
        await queue(f"vid0000000{i}")
    await downloads.start()

    async def all_done():
        return (await db.count_by_status()).get("downloaded") == 3

    await wait_for(all_done)


async def test_paused_pipeline_claims_nothing(services_ctx):
    await queue()
    await downloads.pause()
    await downloads.start()
    await asyncio.sleep(0.15)
    assert (await db.get_video("vid00000001"))["status"] == "pending"
    await downloads.resume()

    async def done():
        return (await db.get_video("vid00000001"))["status"] == "downloaded"

    await wait_for(done)


async def test_concurrency_changes_live(services_ctx, fake_youtube):
    fake_youtube.delay.clear()
    for i in range(4):
        await queue(f"vid0000000{i}")
    await db.set_setting("max_concurrent_downloads", "1")
    await downloads.start()
    await wait_for(lambda: _started(1))
    await asyncio.sleep(0.1)
    assert len(fake_youtube.downloads) == 1
    downloads.set_concurrency(3)
    await wait_for(lambda: _started(3))
    fake_youtube.delay.set()

    async def all_done():
        return (await db.count_by_status()).get("downloaded") == 4

    await wait_for(all_done)
    downloads.set_concurrency(1)
    await asyncio.sleep(0.1)
    assert [i for i, t in downloads._workers.items() if not t.done()] == [0]


async def _started(n):
    return len(rows := await db.list_videos_by_status("downloading")) == n and rows


async def test_stop_cancels_cleanly_and_requeues(services_ctx, fake_youtube):
    fake_youtube.delay.clear()
    await queue()
    await downloads.start()
    await wait_for(lambda: _started(1))
    await downloads.stop()
    fake_youtube.delay.set()
    assert (await db.get_video("vid00000001"))["status"] == "pending"
    assert downloads.get_progress("vid00000001") is None


async def test_manual_link_downloaded_before_older_feed_video(services_ctx):
    await queue("old00000001", channel="UC1")
    await queue("man00000001", force=True)
    assert (await db.claim_next())["video_id"] == "man00000001"


async def test_video_json_shape(services_ctx):
    await queue(channel="UC1", title="Hello")
    (v,) = await subscriptions.list_videos()
    assert v["url"].endswith("watch?v=vid00000001") and v["channel_name"] == "Chan UC1"
    assert v["source_id"] == "UC1" and v["progress"] is None and v["status"] == "pending"
