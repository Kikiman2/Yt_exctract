"""Retention and retry-backoff rules. Pure.

Why: kept apart from the services so the rules (manual videos are never auto-deleted,
per-subscription overrides win over the global setting) are testable without a DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_BACKOFF_HOURS = 7 * 24


def _parse(iso: str) -> datetime:
    """Timestamps are naive UTC (db.now()); an explicit offset is honoured."""
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_expired(row: dict, global_days: int, now_iso: str) -> bool:
    if row.get("channel_id") is None:
        return False
    if row.get("sub_auto_delete") == 0:
        return False
    days = row.get("sub_retention_days")
    if days is None:
        days = global_days
    if days <= 0:
        return False
    downloaded_at = row.get("downloaded_at")
    if not downloaded_at:
        return False
    return _parse(downloaded_at) < _parse(now_iso) - timedelta(days=days)


def backoff_hours(attempts: int, base_hours: int) -> int:
    exponent = min(max(attempts, 1) - 1, 16)  # bounded so huge attempt counts can't overflow
    return min(base_hours * 2**exponent, MAX_BACKOFF_HOURS)
