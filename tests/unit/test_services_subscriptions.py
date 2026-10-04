"""Subscriptions: add/update/remove, refresh error isolation, manual links, video actions."""

import pytest

from app import db
from app.models import LibraryOffline, YouTubeError
from app.services import downloads, subscriptions


async def downloaded(video_id, ctx, sub=None, title="T"):
    """A downloaded row with a real file in the fake library."""
    await db.add_video(video_id, sub, title, channel_title="Chan")
    ctx.writer.write_bytes("YouTube/Chan", f"{title} [{video_id}].mp4", b"x")
    await db.update_video(video_id, {
        "status": "downloaded", "file_path": f"YouTube/Chan/{title} [{video_id}].mp4",
        "downloaded_at": db.now(), "file_size": 1})


async def test_add_source_inserts_refreshes_and_returns_json(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    fake_youtube.publish("UC1", "v0000000001", "First")
    fake_youtube.publish("UC1", "v0000000002", "Second")
    sub = await subscriptions.add_source(" @one ")
    assert sub["id"] == "UC1" and sub["kind"] == "channel" and sub["title"] == "Chan One"
    assert sub["video_count"] == 2 and sub["last_error"] is None and sub["last_checked_at"]
    assert sub["quality"] is None and sub["skip_shorts"] is None and sub["enabled"] is True
    assert any(e["kind"] == "subscribed" for e in await db.list_events())


async def test_add_playlist_source(services_ctx, fake_youtube):
    fake_youtube.add_playlist("PL1", "Mix", "https://www.youtube.com/playlist?list=PL1")
    sub = await subscriptions.add_source("https://www.youtube.com/playlist?list=PL1")
    assert sub["kind"] == "playlist"


async def test_add_source_duplicate_and_bad_input(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    await subscriptions.add_source("@one")
    with pytest.raises(ValueError, match="Already subscribed"):
        await subscriptions.add_source("@one")
    with pytest.raises(ValueError):
        await subscriptions.add_source("   ")
    with pytest.raises(YouTubeError):
        await subscriptions.add_source("@nobody")


async def test_add_source_survives_failing_first_refresh(services_ctx, fake_youtube):
    ref = fake_youtube.add_channel("UC1", "Chan One", "@one")
    fake_youtube.feed_errors["UC1"] = YouTubeError("feed 500")
    sub = await subscriptions.add_source("@one")
    assert sub["id"] == ref.source_id and sub["last_error"] == "feed 500"


async def test_refresh_dedupes_known_videos_and_only_counts_new(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    await subscriptions.add_source("@one")
    fake_youtube.publish("UC1", "v0000000001", "First")
    fake_youtube.publish("UC1", "v0000000001", "First again")  # duplicate inside the feed
    assert await subscriptions.refresh_one("UC1") == 1
    assert await subscriptions.refresh_one("UC1") == 0
    await db.update_video("v0000000001", {"status": "downloaded"})
    assert await subscriptions.refresh_one("UC1") == 0  # downloaded videos are not re-queued
    assert (await db.get_video("v0000000001"))["status"] == "downloaded"


async def test_refresh_keeps_manual_video_untouched(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    await subscriptions.add_source("@one")
    await db.add_video("v0000000001", None, "Manual", force=True)
    fake_youtube.publish("UC1", "v0000000001", "Manual")
    assert await subscriptions.refresh_one("UC1") == 0
    assert (await db.get_video("v0000000001"))["channel_id"] is None


async def test_refresh_error_recorded_once_and_cleared(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    await subscriptions.add_source("@one")
    fake_youtube.feed_errors["UC1"] = YouTubeError("HTTP 500")
    for _ in range(2):
        with pytest.raises(YouTubeError):
            await subscriptions.refresh_one("UC1")
    assert (await db.get_subscription("UC1"))["last_error"] == "HTTP 500"
    failures = [e for e in await db.list_events() if e["kind"] == "refresh_failed"]
    assert len(failures) == 1  # same message is not logged twice
    del fake_youtube.feed_errors["UC1"]
    await subscriptions.refresh_one("UC1")
    assert (await db.get_subscription("UC1"))["last_error"] is None


async def test_refresh_all_isolates_failing_source_and_skips_disabled(services_ctx, fake_youtube):
    for n in ("A", "B", "C"):
        fake_youtube.add_channel(f"UC{n}", f"Chan {n}", f"@{n}")
        await subscriptions.add_source(f"@{n}")
        fake_youtube.publish(f"UC{n}", f"vid{n}0000001", "x")
    fake_youtube.feed_errors["UCA"] = YouTubeError("boom")
    await subscriptions.update_source("UCC", {"enabled": False})
    assert await subscriptions.refresh_all() == 1  # only B
    assert (await db.get_subscription("UCA"))["last_error"] == "boom"
    assert (await db.get_subscription("UCB"))["last_error"] is None
    assert await db.get_video("vidC0000001") is None


async def test_refresh_unknown_source(services_ctx):
    with pytest.raises(KeyError):
        await subscriptions.refresh_one("nope")


async def test_update_source_validation(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    await subscriptions.add_source("@one")
    sub = await subscriptions.update_source(
        "UC1", {"quality": "720p", "skip_shorts": False, "skip_live": True, "retention_days": 14,
                "enabled": False, "auto_delete_enabled": False})
    assert (sub["quality"], sub["skip_shorts"], sub["skip_live"], sub["retention_days"]) == (
        "720p", False, True, 14)
    assert sub["enabled"] is False and sub["auto_delete_enabled"] is False
    sub = await subscriptions.update_source(
        "UC1", {"quality": None, "skip_shorts": None, "skip_live": None, "retention_days": None})
    assert sub["quality"] is None and sub["skip_shorts"] is None and sub["retention_days"] is None
    for bad in ({"quality": "4k"}, {"retention_days": -1}, {"retention_days": 3651},
                {"retention_days": "7"}, {"retention_days": True}, {"skip_shorts": "yes"},
                {"enabled": None}, {"enabled": 1}, {"title": "x"}, {"bogus": 1}):
        with pytest.raises(ValueError):
            await subscriptions.update_source("UC1", bad)
    with pytest.raises(KeyError):
        await subscriptions.update_source("nope", {"enabled": True})


async def test_remove_source_keeps_videos(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    fake_youtube.publish("UC1", "v0000000001", "x")
    await subscriptions.add_source("@one")
    await subscriptions.remove_source("UC1")
    assert await subscriptions.list_sources() == []
    assert (await db.get_video("v0000000001"))["channel_id"] is None
    with pytest.raises(KeyError):
        await subscriptions.remove_source("UC1")


async def test_list_sources_counts(services_ctx, fake_youtube):
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    fake_youtube.publish("UC1", "v0000000001", "x")
    fake_youtube.publish("UC1", "v0000000002", "y")
    await subscriptions.add_source("@one")
    await db.update_video("v0000000001", {"status": "downloaded"})
    (sub,) = await subscriptions.list_sources()
    assert (sub["video_count"], sub["downloaded_count"]) == (2, 1)


async def test_add_video_by_url_new_and_requeue(services_ctx, fake_youtube):
    fake_youtube.add_video("manual00001", "Manual one")
    url = "https://www.youtube.com/watch?v=manual00001"
    v = await subscriptions.add_video_by_url(url)
    assert v["status"] == "pending" and v["source_id"] is None
    assert (await db.get_video("manual00001"))["force_download"] == 1
    await db.update_video("manual00001", {"status": "skipped", "error": "short-form video (30s)"})
    v = await subscriptions.add_video_by_url(url)
    assert v["status"] == "pending" and v["error"] is None
    await db.update_video("manual00001", {"status": "downloading"})
    assert (await subscriptions.add_video_by_url(url))["status"] == "downloading"
    with pytest.raises(YouTubeError):
        await subscriptions.add_video_by_url("https://www.youtube.com/watch?v=unknown")
    with pytest.raises(ValueError):
        await subscriptions.add_video_by_url("")


async def test_add_video_stores_channel_snapshot(services_ctx, fake_youtube):
    ref = fake_youtube.add_channel("UC1", "Chan One")
    fake_youtube.add_video("manual00002", "T", ref)
    v = await subscriptions.add_video_by_url("https://www.youtube.com/watch?v=manual00002")
    assert v["channel_name"] == "Chan One" and v["source_id"] is None


async def test_list_videos_filters(services_ctx, fake_youtube):
    await db.add_subscription("UC1", "channel", "C", "u")
    await db.add_video("a0000000001", "UC1", "a")
    await db.add_video("b0000000002", None, "b")
    await db.update_video("b0000000002", {"status": "failed"})
    assert len(await subscriptions.list_videos()) == 2
    assert [v["video_id"] for v in await subscriptions.list_videos(status="failed")] == ["b0000000002"]
    assert [v["video_id"] for v in await subscriptions.list_videos(source_id="UC1")] == ["a0000000001"]
    assert len(await subscriptions.list_videos(limit=1)) == 1
    with pytest.raises(ValueError):
        await subscriptions.list_videos(status="weird")


async def test_retry_video(services_ctx):
    await db.add_video("a0000000001", None, "a")
    await db.update_video("a0000000001", {"status": "failed", "attempts": 3, "error": "x",
                                          "next_retry_at": "2999-01-01T00:00:00"})
    v = await subscriptions.retry_video("a0000000001", force=True)
    assert v["status"] == "pending" and v["attempts"] == 0 and v["error"] is None
    assert v["next_retry_at"] is None
    assert (await db.get_video("a0000000001"))["force_download"] == 1
    await db.update_video("a0000000001", {"status": "downloading"})
    with pytest.raises(ValueError):
        await subscriptions.retry_video("a0000000001")
    with pytest.raises(KeyError):
        await subscriptions.retry_video("nope")


async def test_delete_video_removes_file_and_marks_deleted(services_ctx, fake_writer, fake_notifier):
    await downloaded("a0000000001", services_ctx)
    v = await subscriptions.delete_video("a0000000001")
    assert v["status"] == "deleted" and v["file_path"] is None
    assert not list(fake_writer.root.rglob("*.mp4"))
    assert fake_notifier.touched == ["YouTube/Chan"]
    # pending rows without a file can be dismissed too
    await db.add_video("b0000000002", None, "b")
    assert (await subscriptions.delete_video("b0000000002"))["status"] == "deleted"


async def test_delete_video_library_offline(services_ctx, fake_writer):
    await downloaded("a0000000001", services_ctx)
    fake_writer.online = False
    with pytest.raises(LibraryOffline):
        await subscriptions.delete_video("a0000000001")
    assert (await db.get_video("a0000000001"))["status"] == "downloaded"


async def test_delete_video_guards(services_ctx):
    with pytest.raises(KeyError):
        await subscriptions.delete_video("nope")
    await db.add_video("a0000000001", None, "a")
    await db.update_video("a0000000001", {"status": "downloading"})
    with pytest.raises(ValueError):
        await subscriptions.delete_video("a0000000001")


async def test_check_missing_requeues_only_missing(services_ctx, fake_writer):
    await downloaded("a0000000001", services_ctx, title="keep")
    await downloaded("b0000000002", services_ctx, title="gone")
    next(fake_writer.root.rglob("gone*")).unlink()
    assert await subscriptions.check_missing() == 1
    assert (await db.get_video("a0000000001"))["status"] == "downloaded"
    b = await db.get_video("b0000000002")
    assert b["status"] == "pending" and b["file_path"] is None


async def test_check_missing_refuses_when_library_offline(services_ctx, fake_writer):
    await downloaded("a0000000001", services_ctx)
    fake_writer.online = False
    with pytest.raises(LibraryOffline):
        await subscriptions.check_missing()
    assert (await db.get_video("a0000000001"))["status"] == "downloaded"


async def test_new_feed_videos_flow_through_pipeline(services_ctx, fake_youtube):
    """End to end with the fakes: subscribe, queue, process, file in the library."""
    fake_youtube.add_channel("UC1", "Chan One", "@one")
    fake_youtube.publish("UC1", "v0000000001", "Hello")
    await subscriptions.add_source("@one")
    await downloads.process_one(await db.claim_next())
    (v,) = await subscriptions.list_videos(status="downloaded")
    assert v["file_path"].startswith("YouTube/Chan One/")
