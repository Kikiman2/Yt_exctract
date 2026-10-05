import sqlite3

import pytest

from app import config, db


@pytest.fixture
async def database():
    await db.init_db(":memory:")
    yield db
    await db.close_db()


async def test_defaults_seeded(database):
    s = await db.get_settings()
    assert s["download_subfolder"] == "YouTube" and s["max_attempts"] == "5"


async def test_legacy_database_migrates_in_place(tmp_path, monkeypatch):
    """A database written by the pre-rewrite app keeps its rows and gains the new columns."""
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE channels (channel_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
            added_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_checked_at TEXT);
        CREATE TABLE videos (video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', added_at TEXT NOT NULL, downloaded_at TEXT,
            file_path TEXT, error TEXT);
        INSERT INTO settings VALUES ('quality', '1080p');
        INSERT INTO channels VALUES ('UC1', 'Chan', 'https://y/c', '2024-01-01T00:00:00.123456', 1, NULL);
        INSERT INTO videos VALUES ('v1', 'UC1', 'T', 'downloading', '2024-01-02T00:00:00', NULL, NULL, NULL);
        """
    )
    old.commit()
    old.close()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    await db.init_db(path)
    try:
        assert (await db.get_settings())["quality"] == "1080p"  # existing value wins
        sub = await db.get_subscription("UC1")
        assert sub["kind"] == "channel" and sub["auto_delete_enabled"] == 1
        v = await db.get_video("v1")
        assert v["status"] == "pending"  # was mid-download in the dead process
        assert v["channel_name"] == "Chan" and v["attempts"] == 0
        await db.init_db(path)  # second start is a no-op migration
    finally:
        await db.close_db()


async def test_claim_order_and_retry(database):
    await db.add_subscription("UC1", "channel", "C", "u")
    await db.add_video("old", "UC1", "old")
    await db.add_video("manual", None, "manual", force=True, channel_title=None)
    first = await db.claim_next()
    assert first["video_id"] == "manual" and first["status"] == "downloading"
    second = await db.claim_next()
    assert second["video_id"] == "old"
    assert await db.claim_next() is None
    await db.update_video("old", {"status": "failed", "attempts": 1, "next_retry_at": "2000-01-01T00:00:00"})
    assert await db.requeue_failed_due(5) == 1
    await db.update_video("old", {"status": "failed", "attempts": 5, "next_retry_at": "2000-01-01T00:00:00"})
    assert await db.requeue_failed_due(5) == 0


async def test_future_retry_not_claimed(database):
    await db.add_video("v", None, "v")
    await db.update_video("v", {"next_retry_at": "2999-01-01T00:00:00"})
    assert await db.claim_next() is None


async def test_remove_subscription_keeps_videos(database):
    await db.add_subscription("UC1", "channel", "C", "u")
    await db.add_video("v", "UC1", "v", channel_title="C")
    await db.remove_subscription("UC1")
    v = await db.get_video("v")
    assert v["channel_id"] is None and v["channel_name"] == "C"


async def test_unknown_fields_rejected(database):
    with pytest.raises(ValueError):
        await db.update_subscription("x", {"nope": 1})
    with pytest.raises(ValueError):
        await db.update_video("x", {"nope": 1})


async def test_events_prune(database):
    for i in range(10):
        await db.add_event("info", "k", f"m{i}")
    await db.prune_events(3)
    assert [e["message"] for e in await db.list_events(10)] == ["m9", "m8", "m7"]
