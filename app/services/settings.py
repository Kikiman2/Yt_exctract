"""User settings stored in the settings table: validation, typed access, and the side
effects of changing them (poll schedule, worker count)."""

from __future__ import annotations

import re

from .. import config, db
from . import context

_INT_KEYS = {
    "poll_interval_minutes": (1, 24 * 60),
    "auto_delete_days": (0, 3650),
    "shorts_max_seconds": (1, 3600),
    "max_concurrent_downloads": (1, 10),
    "retry_failed_after_hours": (1, 24 * 30),
    "max_attempts": (1, 20),
    "min_free_gb": (0, 10_000),
}
_BOOL_KEYS = {"skip_shorts", "skip_live", "write_nfo", "pipeline_paused"}
_BAD_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


async def get_settings() -> dict:
    """SettingsJson: every setting as a string, plus read-only integration info."""
    stored = await db.get_settings()
    values = {k: stored.get(k, v) for k, v in config.DEFAULT_SETTINGS.items()}
    library_path = (
        str(context.ctx().writer.root) if context.has_context() else str(config.MEDIA_ROOT))
    return {
        **values,
        "jellyfin_url": config.JELLYFIN_URL,
        "jellyfin_configured": bool(config.JELLYFIN_API_KEY),
        "library_path": library_path,
    }


def _validate(key: str, value) -> str:
    if key not in config.DEFAULT_SETTINGS:
        raise ValueError(f"unknown setting: {key}")
    if key in _INT_KEYS:
        lo, hi = _INT_KEYS[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a whole number")
        try:
            n = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{key} must be a whole number") from None
        if not lo <= n <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return str(n)
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return "true" if value else "false"
        v = str(value).strip().lower()
        if v not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        return v
    if key == "quality":
        v = str(value).strip()
        if v not in config.QUALITY_FORMATS:
            raise ValueError(f"quality must be one of: {', '.join(config.QUALITY_FORMATS)}")
        return v
    if key == "download_subfolder":
        v = str(value).strip()
        # It becomes a path under the library root, so it must stay one plain folder name.
        if not v or v in (".", "..") or ".." in v or _BAD_FOLDER_CHARS.search(v) or len(v) > 100:
            raise ValueError(
                "download_subfolder must be a single folder name (no slashes, '..' or special characters)")
        return v
    return str(value)


async def update_settings(values: dict) -> dict:
    """Validates everything first (all or nothing), stores, then applies side effects."""
    clean = {k: _validate(k, v) for k, v in values.items()}
    before = await db.get_settings()
    await db.set_settings(clean)
    # Imported here: downloads/scheduler import this module's siblings at load time.
    from .. import scheduler
    from . import downloads

    if "poll_interval_minutes" in clean and clean["poll_interval_minutes"] != before.get("poll_interval_minutes"):
        scheduler.reschedule_poll(int(clean["poll_interval_minutes"]))
    if "max_concurrent_downloads" in clean:
        downloads.set_concurrency(int(clean["max_concurrent_downloads"]))
    changed = {k: v for k, v in clean.items() if before.get(k) != v}
    if changed:
        await db.add_event("info", "settings", "Settings changed: " + ", ".join(sorted(changed)))
    return await get_settings()


def as_int(stored: dict[str, str], key: str) -> int:
    """Typed read of a raw settings dict (db.get_settings()); bad values fall back to the default."""
    try:
        return int(stored.get(key, config.DEFAULT_SETTINGS[key]))
    except ValueError:
        return int(config.DEFAULT_SETTINGS[key])


def as_bool(stored: dict[str, str], key: str) -> bool:
    return stored.get(key, config.DEFAULT_SETTINGS[key]).strip().lower() == "true"
