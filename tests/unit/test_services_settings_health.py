"""Settings validation + side effects, health checks and transitions, cookies service,
upgrade/jellyfin helpers."""

import pytest

from app import config, db
from app.models import CookiesInvalid, YouTubeError
from app.services import cookies, downloads, health, settings


# --- settings ---------------------------------------------------------------------


async def test_get_settings_has_defaults_and_info(services_ctx, fake_writer):
    s = await settings.get_settings()
    assert s["quality"] == "best" and s["jellyfin_configured"] is False
    assert s["library_path"] == str(fake_writer.root) and "jellyfin_url" in s
    assert set(config.DEFAULT_SETTINGS) <= set(s)


async def test_update_settings_roundtrip_normalises(services_ctx):
    s = await settings.update_settings({
        "poll_interval_minutes": " 30 ", "skip_shorts": False, "skip_live": "TRUE",
        "quality": "1080p", "download_subfolder": " Videos ", "max_attempts": 7})
    assert s["poll_interval_minutes"] == "30" and s["skip_shorts"] == "false"
    assert s["skip_live"] == "true" and s["quality"] == "1080p"
    assert s["download_subfolder"] == "Videos" and s["max_attempts"] == "7"
    assert (await db.get_settings())["poll_interval_minutes"] == "30"


@pytest.mark.parametrize("key,value", [
    ("nonsense", "1"),
    ("poll_interval_minutes", "0"), ("poll_interval_minutes", "abc"), ("poll_interval_minutes", "1.5"),
    ("poll_interval_minutes", True), ("poll_interval_minutes", 99999),
    ("auto_delete_days", -1), ("auto_delete_days", 3651),
    ("shorts_max_seconds", 0), ("max_concurrent_downloads", 0), ("max_concurrent_downloads", 11),
    ("retry_failed_after_hours", 0), ("max_attempts", 0), ("min_free_gb", -1),
    ("skip_shorts", "maybe"), ("write_nfo", "1"), ("pipeline_paused", 2),
    ("quality", "4k"), ("quality", ""),
    ("download_subfolder", ""), ("download_subfolder", "a/b"), ("download_subfolder", "a\\b"),
    ("download_subfolder", ".."), ("download_subfolder", "."), ("download_subfolder", "../x"),
    ("download_subfolder", "a..b"), ("download_subfolder", "/abs"), ("download_subfolder", "x" * 101),
    ("download_subfolder", "bad:name"),
])
async def test_update_settings_rejects_bad_values(services_ctx, key, value):
    with pytest.raises(ValueError):
        await settings.update_settings({key: value})


async def test_update_settings_is_all_or_nothing(services_ctx):
    with pytest.raises(ValueError):
        await settings.update_settings({"quality": "720p", "max_attempts": "0"})
    assert (await db.get_settings())["quality"] == "best"


async def test_update_settings_applies_concurrency_and_poll(services_ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(downloads, "set_concurrency", lambda n: seen.__setitem__("n", n))
    from app import scheduler
    monkeypatch.setattr(scheduler, "reschedule_poll", lambda m: seen.__setitem__("m", m))
    await settings.update_settings({"max_concurrent_downloads": "4", "poll_interval_minutes": "45"})
    assert seen == {"n": 4, "m": 45}
    seen.clear()
    await settings.update_settings({"poll_interval_minutes": "45"})  # unchanged: no reschedule
    assert "m" not in seen


async def test_pause_flag_via_settings(services_ctx):
    await settings.update_settings({"pipeline_paused": True})
    assert downloads.USER_PAUSED in await downloads.pause_reasons()
    await settings.update_settings({"pipeline_paused": False})
    assert await downloads.pause_reasons() == []


async def test_scheduler_reschedule_before_start_is_noop():
    from app import scheduler
    scheduler.reschedule_poll(5)


async def test_settings_change_logged(services_ctx):
    await settings.update_settings({"quality": "720p"})
    (e, *_) = await db.list_events()
    assert e["kind"] == "settings" and "quality" in e["message"]


# --- health -----------------------------------------------------------------------


async def test_status_before_first_check_is_not_ok(services_ctx):
    s = await health.status()
    assert s["ok"] is False and s["checked_at"] is None


async def test_check_now_all_good(services_ctx, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)  # POT provider treated ok without network
    h = await health.check_now()
    assert h["ok"] is True and h["ytdlp"] == {"ok": True, "version": "2099.01.01", "detail": "ok"}
    assert h["library"]["ok"] and h["disk"]["ok"] and h["disk"]["free_gb"] > 0
    assert h["jellyfin"] == {"ok": True, "detail": "not configured"}
    assert h["cookies"]["present"] is False and h["checked_at"]
    assert (await health.status())["checked_at"] == h["checked_at"]
    assert await db.list_events() == []  # healthy first check logs nothing


async def test_library_offline_pauses_pipeline_and_logs_transitions(services_ctx, fake_writer, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    await health.check_now()
    fake_writer.online = False
    h = await health.check_now()
    assert h["ok"] is False and h["library"]["ok"] is False
    assert downloads.LIBRARY_OFFLINE in await downloads.pause_reasons()
    await health.check_now()  # still down: no second event
    fake_writer.online = True
    h = await health.check_now()
    assert h["ok"] is True and await downloads.pause_reasons() == []
    msgs = [e["message"] for e in await db.list_events() if e["kind"] == "health"]
    assert msgs == ["library recovered", "library is down: sentinel file missing"]


async def test_first_check_failure_is_logged(services_ctx, fake_writer, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    fake_writer.online = False
    await health.check_now()
    (e,) = await db.list_events()
    assert e["level"] == "warning" and "library is down" in e["message"]


async def test_ytdlp_failure(services_ctx, fake_youtube, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)

    def broken():
        raise YouTubeError("yt-dlp missing")

    monkeypatch.setattr(fake_youtube, "version", broken)
    h = await health.check_now()
    assert h["ok"] is False and h["ytdlp"] == {"ok": False, "version": None, "detail": "yt-dlp missing"}


async def test_low_disk_marks_disk_not_ok(services_ctx, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    await db.set_setting("min_free_gb", "100000000")
    h = await health.check_now()
    assert h["disk"]["ok"] is False and h["ok"] is False
    assert any("Low disk" in r for r in await downloads.pause_reasons())


async def test_pot_provider_checked_over_http(services_ctx, monkeypatch):
    import httpx
    import respx

    monkeypatch.setattr(config, "DEV_FAKE", False)
    with respx.mock:
        route = respx.get(f"{config.POT_PROVIDER_URL}/ping").respond(200, json={"ok": 1})
        assert (await health.check_now())["pot_provider"] == {"ok": True, "detail": "ok"}
        assert route.called
        respx.get(f"{config.POT_PROVIDER_URL}/ping").respond(503)
        assert (await health.check_now())["pot_provider"] == {"ok": False, "detail": "HTTP 503"}
        respx.get(f"{config.POT_PROVIDER_URL}/ping").mock(side_effect=httpx.ConnectError("refused"))
        h = await health.check_now()
        assert h["pot_provider"]["ok"] is False and "refused" in h["pot_provider"]["detail"]
        assert h["ok"] is False


async def test_jellyfin_checked_when_configured(services_ctx, fake_notifier, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    monkeypatch.setattr(config, "JELLYFIN_API_KEY", "key")
    assert (await health.check_now())["jellyfin"] == {"ok": True, "detail": "fake"}
    fake_notifier.ok = False
    h = await health.check_now()
    assert h["jellyfin"]["ok"] is False
    assert h["ok"] is True  # Jellyfin is optional: does not make the app unhealthy


async def test_status_shows_live_cookies(services_ctx, monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", True)
    await health.check_now()
    assert (await health.status())["cookies"]["present"] is False
    await cookies.save(NETSCAPE)
    assert (await health.status())["cookies"]["present"] is True


async def test_events_and_jellyfin_helpers(services_ctx, fake_notifier):
    await db.add_event("info", "a", "one", "v1")
    await db.add_event("info", "b", "two")
    assert [e["message"] for e in await health.list_events(10)] == ["two", "one"]
    assert [e["message"] for e in await health.list_events(10, "v1")] == ["one"]
    assert await health.jellyfin_test() == {"ok": True, "detail": "fake"}
    assert (await health.jellyfin_create_library())["ok"] is True and fake_notifier.created == 1


async def test_upgrade_ytdlp(services_ctx, fake_youtube):
    r = await health.upgrade_ytdlp()
    assert r["version"] == "2099.02.02" and "Restart" in r["note"]
    assert fake_youtube.upgraded_to == "2099.02.02"


# --- cookies ----------------------------------------------------------------------

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tLOGIN_INFO\tdef\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tx\n"
).encode()


async def test_cookies_lifecycle(services_ctx):
    assert (await cookies.status())["present"] is False
    s = await cookies.save(NETSCAPE)
    assert s["present"] and s["cookie_count"] == 3 and s["youtube_cookie_count"] == 3
    assert s["expires_at"] and config.COOKIES_FILE.exists()
    assert config.COOKIES_FILE.stat().st_mode & 0o777 == 0o600
    assert not config.COOKIES_FILE.with_name("cookies.txt.part").exists()
    s = await cookies.delete()
    assert s["present"] is False and not config.COOKIES_FILE.exists()
    await cookies.delete()  # deleting nothing is fine
    kinds = [e["kind"] for e in await db.list_events()]
    assert kinds.count("cookies") == 3


async def test_cookies_invalid_upload_keeps_existing(services_ctx):
    await cookies.save(NETSCAPE)
    with pytest.raises(CookiesInvalid):
        await cookies.save(b"not cookies at all")
    with pytest.raises(ValueError):  # CookiesInvalid is a ValueError -> 400
        await cookies.save(b"")
    assert (await cookies.status())["cookie_count"] == 3
