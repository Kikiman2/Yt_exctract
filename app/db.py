import datetime

import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    added_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_delete_enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    added_at TEXT NOT NULL,
    downloaded_at TEXT,
    file_path TEXT,
    error TEXT,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);
"""

_connection: aiosqlite.Connection | None = None


def now() -> str:
    return datetime.datetime.utcnow().isoformat()


async def init_db() -> None:
    global _connection
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(config.DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(SCHEMA)
    try:
        await _connection.execute("ALTER TABLE videos ADD COLUMN deleted_at TEXT")
    except aiosqlite.OperationalError:
        pass  # column already exists
    try:
        await _connection.execute(
            "ALTER TABLE videos ADD COLUMN force_download INTEGER NOT NULL DEFAULT 0"
        )
    except aiosqlite.OperationalError:
        pass  # column already exists
    try:
        await _connection.execute(
            "ALTER TABLE channels ADD COLUMN auto_delete_enabled INTEGER NOT NULL DEFAULT 1"
        )
    except aiosqlite.OperationalError:
        pass  # column already exists
    for key, value in config.DEFAULT_SETTINGS.items():
        await _connection.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    # A fresh process can't have anything genuinely mid-download; requeue rows
    # left in 'downloading' by a previous run that didn't shut down cleanly.
    await _connection.execute(
        "UPDATE videos SET status = 'pending' WHERE status = 'downloading'"
    )
    await _connection.commit()


async def close_db() -> None:
    if _connection is not None:
        await _connection.close()


def get_conn() -> aiosqlite.Connection:
    assert _connection is not None, "DB not initialized"
    return _connection


# --- settings ---

async def get_settings() -> dict[str, str]:
    conn = get_conn()
    cursor = await conn.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


async def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


# --- channels ---

async def add_channel(channel_id: str, title: str, url: str) -> None:
    conn = get_conn()
    await conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, title, url, added_at, enabled) "
        "VALUES (?, ?, ?, ?, 1)",
        (channel_id, title, url, now()),
    )
    await conn.commit()


async def remove_channel(channel_id: str) -> None:
    conn = get_conn()
    await conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    await conn.commit()


async def set_channel_enabled(channel_id: str, enabled: bool) -> None:
    conn = get_conn()
    await conn.execute(
        "UPDATE channels SET enabled = ? WHERE channel_id = ?", (1 if enabled else 0, channel_id)
    )
    await conn.commit()


async def set_channel_auto_delete(channel_id: str, enabled: bool) -> None:
    conn = get_conn()
    await conn.execute(
        "UPDATE channels SET auto_delete_enabled = ? WHERE channel_id = ?",
        (1 if enabled else 0, channel_id),
    )
    await conn.commit()


async def touch_channel_checked(channel_id: str) -> None:
    conn = get_conn()
    await conn.execute(
        "UPDATE channels SET last_checked_at = ? WHERE channel_id = ?", (now(), channel_id)
    )
    await conn.commit()


async def list_channels() -> list[aiosqlite.Row]:
    conn = get_conn()
    cursor = await conn.execute("SELECT * FROM channels ORDER BY added_at DESC")
    return await cursor.fetchall()


async def list_enabled_channels() -> list[aiosqlite.Row]:
    conn = get_conn()
    cursor = await conn.execute("SELECT * FROM channels WHERE enabled = 1")
    return await cursor.fetchall()


# --- videos ---

async def add_pending_video(
    video_id: str, channel_id: str | None, title: str, force: bool = False
) -> bool:
    """Returns True if a new row was inserted (i.e. video wasn't already known)."""
    conn = get_conn()
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO videos (video_id, channel_id, title, status, added_at, force_download) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (video_id, channel_id, title, now(), 1 if force else 0),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def get_video(video_id: str) -> aiosqlite.Row | None:
    conn = get_conn()
    cursor = await conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    return await cursor.fetchone()


async def claim_next_pending_video() -> dict | None:
    """Atomically pick the oldest pending video and mark it 'downloading', so
    multiple concurrent workers can't claim the same row."""
    conn = get_conn()
    cursor = await conn.execute(
        "UPDATE videos SET status = 'downloading' "
        "WHERE video_id = ("
        "  SELECT video_id FROM videos WHERE status = 'pending' ORDER BY added_at ASC LIMIT 1"
        ") "
        "RETURNING *"
    )
    row = await cursor.fetchone()
    await conn.commit()
    if row is None:
        return None
    video = dict(row)
    channel_cursor = await conn.execute(
        "SELECT title FROM channels WHERE channel_id = ?", (video["channel_id"],)
    )
    channel_row = await channel_cursor.fetchone()
    video["channel_title"] = channel_row["title"] if channel_row else None
    return video


async def set_video_status(
    video_id: str,
    status: str,
    file_path: str | None = None,
    error: str | None = None,
    clear_force: bool = False,
) -> None:
    conn = get_conn()
    force_clause = ", force_download = 0" if clear_force else ""
    if status == "downloaded":
        await conn.execute(
            f"UPDATE videos SET status = ?, file_path = ?, downloaded_at = ?, error = NULL{force_clause} "
            "WHERE video_id = ?",
            (status, file_path, now(), video_id),
        )
    elif status == "deleted":
        await conn.execute(
            f"UPDATE videos SET status = ?, file_path = NULL, deleted_at = ?, error = NULL{force_clause} "
            "WHERE video_id = ?",
            (status, now(), video_id),
        )
    else:
        await conn.execute(
            f"UPDATE videos SET status = ?, error = ?{force_clause} WHERE video_id = ?",
            (status, error, video_id),
        )
    await conn.commit()


async def retry_video(video_id: str, force: bool = False) -> None:
    conn = get_conn()
    await conn.execute(
        "UPDATE videos SET status = 'pending', error = NULL, force_download = ? WHERE video_id = ?",
        (1 if force else 0, video_id),
    )
    await conn.commit()


async def list_stale_downloaded_videos(cutoff: str) -> list[aiosqlite.Row]:
    """Videos still marked downloaded whose downloaded_at is older than cutoff (ISO string).

    Only videos belonging to a channel with auto-delete enabled are returned; videos
    added manually (no channel_id) are never auto-deleted, since there's no channel
    row to opt them in.
    """
    conn = get_conn()
    cursor = await conn.execute(
        "SELECT videos.* FROM videos "
        "JOIN channels ON videos.channel_id = channels.channel_id "
        "WHERE videos.status = 'downloaded' AND videos.downloaded_at IS NOT NULL "
        "AND videos.downloaded_at < ? AND channels.auto_delete_enabled = 1",
        (cutoff,),
    )
    return await cursor.fetchall()


async def list_videos_by_status(status: str) -> list[aiosqlite.Row]:
    conn = get_conn()
    cursor = await conn.execute("SELECT * FROM videos WHERE status = ?", (status,))
    return await cursor.fetchall()


async def list_videos(limit: int = 200) -> list[aiosqlite.Row]:
    conn = get_conn()
    cursor = await conn.execute(
        "SELECT videos.*, channels.title AS channel_title FROM videos "
        "LEFT JOIN channels ON videos.channel_id = channels.channel_id "
        "ORDER BY added_at DESC LIMIT ?",
        (limit,),
    )
    return await cursor.fetchall()
