"""SQLite storage (single aiosqlite connection, WAL). Every function returns plain
dicts. Services go through here; adapters never touch the database.

Compatibility: the `settings`, `channels` and `videos` tables of the pre-rewrite app
are kept as they were (a "subscription" is a row of `channels`, a playlist has
kind='playlist'); new columns are added by `_migrate`, never by dropping data."""

import datetime
import json

import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,          -- UC... for channels, PL... for playlists
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

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,                  -- info|warning|error
    kind TEXT NOT NULL,
    video_id TEXT,
    message TEXT NOT NULL
);
"""

# (table, column, DDL) added on top of the original schema. Idempotent.
COLUMNS = [
    ("videos", "deleted_at", "TEXT"),
    ("videos", "force_download", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "auto_delete_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("channels", "kind", "TEXT NOT NULL DEFAULT 'channel'"),
    ("channels", "last_error", "TEXT"),
    # Per-subscription overrides; NULL = use the global setting.
    ("channels", "quality", "TEXT"),
    ("channels", "skip_shorts", "INTEGER"),
    ("channels", "skip_live", "INTEGER"),
    ("channels", "retention_days", "INTEGER"),
    ("videos", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("videos", "next_retry_at", "TEXT"),
    ("videos", "started_at", "TEXT"),
    ("videos", "file_size", "INTEGER"),
    ("videos", "upload_date", "TEXT"),
    ("videos", "duration", "INTEGER"),
    ("videos", "channel_title", "TEXT"),   # snapshot, so manual videos have a folder name
]

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(id);
"""

# Columns of `channels` a caller may change through update_subscription().
SUBSCRIPTION_FIELDS = {
    "title", "enabled", "auto_delete_enabled", "quality", "skip_shorts",
    "skip_live", "retention_days", "last_checked_at", "last_error",
}
VIDEO_FIELDS = {
    "title", "status", "downloaded_at", "file_path", "error", "deleted_at",
    "force_download", "attempts", "next_retry_at", "started_at", "file_size",
    "upload_date", "duration", "channel_title", "channel_id",
}

_connection: aiosqlite.Connection | None = None


def now() -> str:
    """UTC, naive ISO to the second (same shape as the pre-rewrite rows, so string
    comparison keeps working)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def loads(text: str | None, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"))


async def init_db(path=None) -> None:
    """`path=":memory:"` for tests. Safe to call on an existing pre-rewrite database."""
    global _connection
    if _connection is not None:
        await close_db()
    target = str(path) if path is not None else str(config.DB_PATH)
    if target != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(target)
    _connection.row_factory = aiosqlite.Row
    await _connection.execute("PRAGMA journal_mode = WAL")
    await _connection.execute("PRAGMA foreign_keys = ON")
    await _connection.executescript(SCHEMA)
    await _migrate(_connection)
    for key, value in config.DEFAULT_SETTINGS.items():
        await _connection.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    # A fresh process can't have anything genuinely mid-download.
    await _connection.execute(
        "UPDATE videos SET status = 'pending', started_at = NULL WHERE status = 'downloading'"
    )
    await _connection.commit()


async def _migrate(conn: aiosqlite.Connection) -> None:
    for table, column, ddl in COLUMNS:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        have = {r["name"] for r in await cur.fetchall()}
        if column not in have:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await conn.executescript(INDEXES)


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def get_conn() -> aiosqlite.Connection:
    assert _connection is not None, "DB not initialized"
    return _connection


def _d(row) -> dict | None:
    return dict(row) if row is not None else None


async def _all(sql: str, params: tuple = ()) -> list[dict]:
    cur = await get_conn().execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def _one(sql: str, params: tuple = ()) -> dict | None:
    cur = await get_conn().execute(sql, params)
    return _d(await cur.fetchone())


# --- settings ---------------------------------------------------------------------


async def get_settings() -> dict[str, str]:
    return {r["key"]: r["value"] for r in await _all("SELECT key, value FROM settings")}


async def set_setting(key: str, value: str) -> None:
    await set_settings({key: value})


async def set_settings(values: dict[str, str]) -> None:
    conn = get_conn()
    for key, value in values.items():
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    await conn.commit()


# --- events -----------------------------------------------------------------------


async def add_event(level: str, kind: str, message: str, video_id: str | None = None) -> None:
    conn = get_conn()
    await conn.execute(
        "INSERT INTO events (ts, level, kind, video_id, message) VALUES (?, ?, ?, ?, ?)",
        (now(), level, kind, video_id, message[:2000]),
    )
    await conn.commit()


async def list_events(limit: int = 100, video_id: str | None = None) -> list[dict]:
    if video_id is not None:
        return await _all(
            "SELECT * FROM events WHERE video_id = ? ORDER BY id DESC LIMIT ?", (video_id, limit))
    return await _all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


async def prune_events(keep: int) -> None:
    conn = get_conn()
    await conn.execute(
        "DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM events) - ?", (keep,))
    await conn.commit()


# --- subscriptions (table `channels`) ---------------------------------------------


async def add_subscription(source_id: str, kind: str, title: str, url: str) -> bool:
    """True if inserted, False if it already existed."""
    conn = get_conn()
    cur = await conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, kind, title, url, added_at, enabled) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (source_id, kind, title, url, now()),
    )
    await conn.commit()
    return cur.rowcount > 0


async def get_subscription(source_id: str) -> dict | None:
    return await _one("SELECT * FROM channels WHERE channel_id = ?", (source_id,))


async def list_subscriptions() -> list[dict]:
    """Each row also has `video_count` and `downloaded_count`."""
    return await _all(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id) AS video_count, "
        "(SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id "
        " AND v.status = 'downloaded') AS downloaded_count "
        "FROM channels c ORDER BY c.added_at DESC"
    )


async def list_enabled_subscriptions() -> list[dict]:
    return await _all("SELECT * FROM channels WHERE enabled = 1 ORDER BY added_at")


async def update_subscription(source_id: str, fields: dict) -> None:
    bad = set(fields) - SUBSCRIPTION_FIELDS
    if bad:
        raise ValueError(f"unknown subscription field(s): {', '.join(sorted(bad))}")
    if not fields:
        return
    conn = get_conn()
    sets = ", ".join(f"{k} = ?" for k in fields)
    await conn.execute(
        f"UPDATE channels SET {sets} WHERE channel_id = ?", (*fields.values(), source_id))
    await conn.commit()


async def remove_subscription(source_id: str) -> None:
    """Deletes the subscription; its videos stay (channel_id set to NULL) so history
    and files remain manageable."""
    conn = get_conn()
    await conn.execute("UPDATE videos SET channel_id = NULL WHERE channel_id = ?", (source_id,))
    await conn.execute("DELETE FROM channels WHERE channel_id = ?", (source_id,))
    await conn.commit()


# --- videos -----------------------------------------------------------------------


async def add_video(
    video_id: str,
    subscription_id: str | None,
    title: str,
    force: bool = False,
    channel_title: str | None = None,
) -> bool:
    """True if a new row was inserted (the video wasn't already known)."""
    conn = get_conn()
    cur = await conn.execute(
        "INSERT OR IGNORE INTO videos "
        "(video_id, channel_id, title, status, added_at, force_download, channel_title) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (video_id, subscription_id, title, now(), 1 if force else 0, channel_title),
    )
    await conn.commit()
    return cur.rowcount > 0


async def get_video(video_id: str) -> dict | None:
    return await _one(_VIDEO_SELECT + " WHERE v.video_id = ?", (video_id,))


# Joined view used by lists/claims: the subscription's title and overrides come along.
_VIDEO_SELECT = (
    "SELECT v.*, COALESCE(c.title, v.channel_title) AS channel_name, c.kind AS source_kind, "
    "c.quality AS sub_quality, c.skip_shorts AS sub_skip_shorts, "
    "c.skip_live AS sub_skip_live, c.retention_days AS sub_retention_days, "
    "c.auto_delete_enabled AS sub_auto_delete "
    "FROM videos v LEFT JOIN channels c ON v.channel_id = c.channel_id"
)


async def list_videos(
    limit: int = 200,
    offset: int = 0,
    status: str | None = None,
    subscription_id: str | None = None,
) -> list[dict]:
    where, params = [], []
    if status:
        where.append("v.status = ?")
        params.append(status)
    if subscription_id:
        where.append("v.channel_id = ?")
        params.append(subscription_id)
    sql = _VIDEO_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY v.added_at DESC, v.video_id LIMIT ? OFFSET ?"
    return await _all(sql, (*params, limit, offset))


async def count_by_status() -> dict[str, int]:
    rows = await _all("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


async def claim_next() -> dict | None:
    """Atomically take the next due pending video (manual links first, then oldest)
    and mark it 'downloading'. Returns the joined row, or None.

    SELECT then a conditional UPDATE (retry if another worker won the row) rather than
    UPDATE ... RETURNING: with one shared connection, a RETURNING cursor that is still
    open makes a concurrent commit by another coroutine fail ("SQL statements in progress")."""
    conn = get_conn()
    while True:
        ts = now()
        cur = await conn.execute(
            "SELECT video_id FROM videos WHERE status = 'pending' "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "ORDER BY force_download DESC, added_at ASC, video_id LIMIT 1",
            (ts,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        cur = await conn.execute(
            "UPDATE videos SET status = 'downloading', started_at = ? "
            "WHERE video_id = ? AND status = 'pending'",
            (ts, row["video_id"]),
        )
        await conn.commit()
        if cur.rowcount > 0:
            return await get_video(row["video_id"])


async def update_video(video_id: str, fields: dict) -> None:
    bad = set(fields) - VIDEO_FIELDS
    if bad:
        raise ValueError(f"unknown video field(s): {', '.join(sorted(bad))}")
    if not fields:
        return
    conn = get_conn()
    sets = ", ".join(f"{k} = ?" for k in fields)
    await conn.execute(f"UPDATE videos SET {sets} WHERE video_id = ?", (*fields.values(), video_id))
    await conn.commit()


async def requeue(video_id: str, force: bool = False, reset_attempts: bool = True) -> None:
    """Back to 'pending' (manual retry / re-download)."""
    extra = ", attempts = 0" if reset_attempts else ""
    conn = get_conn()
    await conn.execute(
        "UPDATE videos SET status = 'pending', error = NULL, next_retry_at = NULL, "
        f"force_download = ?{extra} WHERE video_id = ?",
        (1 if force else 0, video_id),
    )
    await conn.commit()


async def requeue_failed_due(max_attempts: int) -> int:
    """failed videos whose next_retry_at has passed and that still have attempts left
    go back to 'pending'. Returns how many."""
    conn = get_conn()
    cur = await conn.execute(
        "UPDATE videos SET status = 'pending' WHERE status = 'failed' "
        "AND next_retry_at IS NOT NULL AND next_retry_at <= ? AND attempts < ?",
        (now(), max_attempts),
    )
    await conn.commit()
    return cur.rowcount


async def list_downloaded() -> list[dict]:
    """Every 'downloaded' video, joined with its subscription's retention fields
    (sub_retention_days, sub_auto_delete). Manual videos have both as NULL."""
    return await _all(_VIDEO_SELECT + " WHERE v.status = 'downloaded'")


async def list_videos_by_status(status: str) -> list[dict]:
    return await _all(_VIDEO_SELECT + " WHERE v.status = ?", (status,))
