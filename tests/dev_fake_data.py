"""Demo data for DEV_FAKE=1: a scriptable FakeYouTube that resolves a few channels, a
fake library under DATA_DIR, and seeded subscriptions/videos/events so every UI state
(downloaded, queued, failed, skipped, deleted) is visible without yt-dlp."""

from __future__ import annotations

import datetime

from app import config, db
from app.domain import naming
from app.services.context import Context
from tests.fakes import FakeLibraryWriter, FakeNotifier, FakeYouTube


def build_context() -> Context:
    yt = FakeYouTube()
    tech = yt.add_channel("UCdemoTech", "Demo Tech", "@demotech", "demotech")
    yt.add_channel("UCdemoCooking", "Demo Cooking", "@democooking")
    yt.add_playlist("PLdemoMix", "Demo Mix Playlist")
    yt.add_video("manual00001", "A manually added video")
    yt.add_video("manual00002", "Another manual video", tech)
    yt.publish("UCdemoTech", "technew0001", "Brand new upload")
    return Context(youtube=yt, writer=FakeLibraryWriter(config.DATA_DIR / "fake-library"),
                   notifier=FakeNotifier())


def _ago(days: float) -> str:
    when = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=days)
    return when.isoformat(timespec="seconds")


async def seed_db(ctx: Context) -> None:
    """Idempotent: does nothing once any subscription exists."""
    if await db.list_subscriptions():
        return
    conn = db.get_conn()
    subfolder = (await db.get_settings())["download_subfolder"]
    await db.add_subscription("UCdemoTech", "channel", "Demo Tech", "https://www.youtube.com/channel/UCdemoTech")
    await db.add_subscription("UCdemoCooking", "channel", "Demo Cooking",
                              "https://www.youtube.com/channel/UCdemoCooking")
    await db.add_subscription("PLdemoMix", "playlist", "Demo Mix Playlist",
                              "https://www.youtube.com/playlist?list=PLdemoMix")
    await db.update_subscription("UCdemoCooking", {"quality": "720p", "retention_days": 7, "skip_shorts": 0})
    await db.update_subscription("PLdemoMix", {"enabled": 0, "last_error": "HTTP 404 from the feed"})
    await db.update_subscription("UCdemoTech", {"last_checked_at": db.now()})

    for i, (vid, title, days) in enumerate([
        ("demo_done_1", "Building a home server", 2),
        ("demo_done_2", "Ten shell tricks", 20),
    ]):
        rel_folder = naming.rel_folder(subfolder, "Demo Tech")
        name = naming.video_file_name(title, vid)
        content = b"demo-video-" + vid.encode()
        ctx.writer.write_bytes(rel_folder, name, content)
        await db.add_video(vid, "UCdemoTech", title, channel_title="Demo Tech")
        await db.update_video(vid, {
            "status": "downloaded", "downloaded_at": _ago(days), "file_path": f"{rel_folder}/{name}",
            "file_size": len(content), "duration": 600 + i * 300, "upload_date": "2024-05-06"})
    await db.add_video("demo_pend_1", "UCdemoCooking", "Sourdough from scratch", channel_title="Demo Cooking")
    await db.add_video("demo_pend_2", "UCdemoTech", "Queued upload", channel_title="Demo Tech")
    await db.add_video("demo_fail_1", "UCdemoCooking", "Private broadcast", channel_title="Demo Cooking")
    await db.update_video("demo_fail_1", {
        "status": "failed", "attempts": 2, "error": "Video unavailable", "next_retry_at": _ago(-0.25)})
    await db.add_video("demo_skip_1", "UCdemoTech", "Quick tip #shorts", channel_title="Demo Tech")
    await db.update_video("demo_skip_1", {"status": "skipped", "error": "short-form video (45s)"})
    await db.add_video("demo_del_1", "UCdemoTech", "Expired video", channel_title="Demo Tech")
    await db.update_video("demo_del_1", {"status": "deleted", "deleted_at": _ago(1)})
    await db.add_video("manual00009", None, "Manual link", force=True, channel_title="Someone")
    await conn.commit()

    await db.add_event("info", "subscribed", "Subscribed to channel Demo Tech")
    await db.add_event("error", "download_failed", "Video unavailable (attempt 2/5)", "demo_fail_1")
    await db.add_event("warning", "refresh_failed", "Demo Mix Playlist: HTTP 404 from the feed")
