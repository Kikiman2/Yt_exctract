import pytest

from app.domain.retention import backoff_hours, is_expired

NOW = "2026-10-04T12:00:00"


def row(**kw):
    base = {"channel_id": "UC1", "downloaded_at": "2026-08-01T00:00:00",
            "sub_retention_days": None, "sub_auto_delete": 1}
    return {**base, **kw}


def test_expired_by_global_days():
    assert is_expired(row(), 30, NOW)


def test_not_expired_inside_window():
    assert not is_expired(row(downloaded_at="2026-09-20T00:00:00"), 30, NOW)


def test_boundary_is_strictly_older():
    assert not is_expired(row(downloaded_at="2026-09-04T12:00:00"), 30, NOW)
    assert is_expired(row(downloaded_at="2026-09-04T11:59:59"), 30, NOW)


def test_manual_video_never_expires():
    assert not is_expired(row(channel_id=None, sub_auto_delete=None), 1, NOW)


def test_auto_delete_off_never_expires():
    assert not is_expired(row(sub_auto_delete=0), 1, NOW)


def test_sub_override_shorter_than_global():
    r = row(downloaded_at="2026-09-20T00:00:00", sub_retention_days=7)
    assert is_expired(r, 365, NOW)


def test_sub_override_longer_than_global():
    assert not is_expired(row(sub_retention_days=365), 30, NOW)


def test_sub_override_zero_keeps_forever():
    assert not is_expired(row(sub_retention_days=0), 30, NOW)


def test_global_zero_keeps_forever():
    assert not is_expired(row(), 0, NOW)


def test_sub_override_beats_global_zero():
    assert is_expired(row(sub_retention_days=10), 0, NOW)


def test_negative_days_never_expire():
    assert not is_expired(row(), -5, NOW)


def test_missing_downloaded_at():
    assert not is_expired(row(downloaded_at=None), 30, NOW)


def test_missing_subscription_row_uses_global():
    assert is_expired(row(sub_auto_delete=None), 30, NOW)


def test_timezone_suffixes_accepted():
    assert is_expired(row(downloaded_at="2026-08-01T00:00:00+00:00"), 30, "2026-10-04T12:00:00Z")


@pytest.mark.parametrize("attempts,expected", [(1, 6), (2, 12), (3, 24), (4, 48), (5, 96), (6, 168), (7, 168)])
def test_backoff(attempts, expected):
    assert backoff_hours(attempts, 6) == expected


def test_backoff_cap_with_huge_attempts():
    assert backoff_hours(10_000, 6) == 168


def test_backoff_attempts_below_one_treated_as_first():
    assert backoff_hours(0, 6) == 6


def test_backoff_zero_base():
    assert backoff_hours(3, 0) == 0
