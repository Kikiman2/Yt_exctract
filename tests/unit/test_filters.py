import pytest

from app.domain.filters import skip_reason


def run(info, *, shorts=True, max_s=180, live=True, force=False):
    return skip_reason(info, skip_shorts=shorts, shorts_max_seconds=max_s, skip_live=live, force=force)


def test_normal_video_passes():
    assert run({"duration": 600, "live_status": "not_live"}) is None


@pytest.mark.parametrize("status,text", [("is_live", "live stream (is live)"), ("is_upcoming", "live stream (is upcoming)")])
def test_live_skipped(status, text):
    assert run({"live_status": status, "duration": 600}) == text


def test_live_allowed_when_setting_off():
    assert run({"live_status": "is_live"}, live=False) is None


def test_was_live_is_not_skipped():
    assert run({"live_status": "was_live", "duration": 7200}) is None


def test_short_by_media_type():
    assert run({"media_type": "short", "duration": 600}) == "short-form video (600s)"


def test_media_type_video_overrides_short_duration():
    assert run({"media_type": "video", "duration": 30, "webpage_url": "https://youtube.com/shorts/x"}) is None


def test_short_by_duration_fallback():
    assert run({"duration": 60}) == "short-form video (60s)"
    assert run({"duration": 180}) == "short-form video (180s)"
    assert run({"duration": 181}) is None


def test_short_by_url_fallback():
    assert run({"webpage_url": "https://www.youtube.com/shorts/abc", "duration": 500}) == "short-form video (500s)"


def test_short_without_duration():
    assert run({"media_type": "short"}) == "short-form video"


def test_unknown_duration_not_short():
    assert run({}) is None


def test_shorts_allowed_when_setting_off():
    assert run({"media_type": "short", "duration": 10}, shorts=False) is None


def test_live_checked_before_short():
    assert run({"live_status": "is_live", "duration": 10}).startswith("live stream")


def test_force_bypasses_everything():
    assert run({"live_status": "is_live", "media_type": "short", "duration": 5}, force=True) is None


def test_custom_max_seconds():
    assert run({"duration": 200}, max_s=300) == "short-form video (200s)"
