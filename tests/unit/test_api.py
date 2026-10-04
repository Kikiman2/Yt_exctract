import pytest
from fastapi.testclient import TestClient

from app import auth, config, models
from app.main import create_app
from app.services import context, contract
from tests import dev_fake_data

BASE = "http://testserver"


@pytest.fixture
async def fakes(monkeypatch, database):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(config, "SESSION_SECRET", "secret")
    monkeypatch.setattr(config, "DEV_FAKE", False)
    auth.rate_limiter.reset()
    ctx = dev_fake_data.build_context()
    context.set_context(ctx)
    await dev_fake_data.seed_db(ctx)
    return ctx


@pytest.fixture
def client(fakes):
    c = TestClient(create_app(), base_url=BASE, follow_redirects=False, headers={"Origin": BASE})
    assert c.post("/login", data={"username": "admin", "password": "pw"}).status_code == 303
    return c


def assert_shape(obj: dict, typed_dict):
    expected = set()
    for cls in reversed(typed_dict.__mro__):
        expected |= set(getattr(cls, "__annotations__", {}))
    assert set(obj) == expected, set(obj) ^ expected


# --- pages -------------------------------------------------------------------------


@pytest.mark.parametrize("path,marker", [
    ("/", "dashboard.js"), ("/sources", "sources.js"), ("/settings", "settings.js"), ("/health", "health.js")])
def test_pages_render(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200
    assert marker in r.text


# --- status / queue ----------------------------------------------------------------


def test_status_shape(client):
    data = client.get("/api/status").json()
    assert_shape(data["health"], contract.HealthJson)
    assert_shape(data["queue"], contract.QueueJson)
    assert set(data["queue"]["counts"]) == set(models.VIDEO_STATUSES)


def test_queue_pause_resume(client):
    r = client.post("/api/queue/pause")
    assert r.status_code == 200
    assert r.json()["paused"] is True and "Paused by user" in r.json()["pause_reasons"]
    r = client.post("/api/queue/resume")
    assert "Paused by user" not in r.json()["pause_reasons"]


# --- videos ------------------------------------------------------------------------


def test_list_videos(client):
    videos = client.get("/api/videos").json()
    assert videos
    for v in videos:
        assert_shape(v, contract.VideoJson)
    failed = client.get("/api/videos", params={"status": "failed"}).json()
    assert [v["video_id"] for v in failed] == ["demo_fail_1"]
    by_source = client.get("/api/videos", params={"source_id": "UCdemoCooking"}).json()
    assert {v["source_id"] for v in by_source} == {"UCdemoCooking"}
    assert len(client.get("/api/videos", params={"limit": 2}).json()) == 2


def test_list_videos_errors(client):
    r = client.get("/api/videos", params={"status": "bogus"})
    assert r.status_code == 400 and "bogus" in r.json()["detail"]
    assert client.get("/api/videos", params={"limit": 0}).status_code == 422


def test_add_video(client):
    r = client.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=manual00001"})
    assert r.status_code == 200, r.text
    assert_shape(r.json(), contract.VideoJson)
    assert r.json()["video_id"] == "manual00001" and r.json()["source_id"] is None


def test_add_video_errors(client):
    r = client.post("/api/videos", json={"url": "https://www.youtube.com/watch?v=nope"})
    assert r.status_code == 502 and "Could not resolve" in r.json()["detail"]
    assert client.post("/api/videos", json={"url": "  "}).status_code == 400
    assert client.post("/api/videos", json={}).status_code == 422


def test_retry_video(client):
    r = client.post("/api/videos/demo_fail_1/retry", json={})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = client.post("/api/videos/demo_skip_1/retry", json={"force": True})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert client.post("/api/videos/demo_done_1/retry").status_code == 200  # body optional
    assert client.post("/api/videos/nope/retry", json={}).status_code == 404


def test_delete_video(client, fakes):
    row = client.get("/api/videos", params={"status": "downloaded"}).json()[0]
    assert fakes.writer.exists(row["file_path"])
    r = client.post(f"/api/videos/{row['video_id']}/delete", json={})
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert not fakes.writer.exists(row["file_path"])
    assert client.post("/api/videos/nope/delete", json={}).status_code == 404


def test_delete_video_library_offline(client, fakes):
    fakes.writer.online = False
    r = client.post("/api/videos/demo_done_1/delete", json={})
    assert r.status_code == 503 and "offline" in r.json()["detail"]


def test_check_missing(client, fakes):
    assert client.post("/api/videos/check-missing").json() == {"requeued": 0}
    path = client.get("/api/videos", params={"status": "downloaded"}).json()[0]["file_path"]
    (fakes.writer.root / path).unlink()
    assert client.post("/api/videos/check-missing").json() == {"requeued": 1}
    fakes.writer.online = False
    assert client.post("/api/videos/check-missing").status_code == 503


# --- sources -----------------------------------------------------------------------


def test_list_sources(client):
    sources = client.get("/api/sources").json()
    assert {s["id"] for s in sources} == {"UCdemoTech", "UCdemoCooking", "PLdemoMix"}
    for s in sources:
        assert_shape(s, contract.SubscriptionJson)


def test_add_source(client):
    r = client.post("/api/sources", json={"url": "@demotech"})
    assert r.status_code == 400 and "Already subscribed" in r.json()["detail"]
    fakes_yt = context.ctx().youtube
    fakes_yt.add_channel("UCnew", "New Channel", "@newchannel")
    r = client.post("/api/sources", json={"url": "@newchannel"})
    assert r.status_code == 200, r.text
    assert_shape(r.json(), contract.SubscriptionJson)
    assert r.json()["title"] == "New Channel"
    r = client.post("/api/sources", json={"url": "@unknown"})
    assert r.status_code == 502


def test_patch_source(client):
    r = client.patch("/api/sources/UCdemoTech", json={"enabled": False, "quality": "720p", "retention_days": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["quality"] == "720p" and body["retention_days"] == 5
    assert client.patch("/api/sources/UCdemoTech", json={"quality": None}).json()["quality"] is None
    r = client.patch("/api/sources/UCdemoTech", json={"quality": "4k"})
    assert r.status_code == 400 and "quality" in r.json()["detail"]
    assert client.patch("/api/sources/UCdemoTech", json={"hacker": 1}).status_code == 400
    assert client.patch("/api/sources/nope", json={"enabled": True}).status_code == 404
    assert client.patch("/api/sources/UCdemoTech", content="nope",
                        headers={"Content-Type": "application/json"}).status_code == 422


def test_delete_source(client):
    assert client.delete("/api/sources/UCdemoCooking").json() == {"ok": True}
    assert client.delete("/api/sources/UCdemoCooking").status_code == 404
    assert "UCdemoCooking" not in {s["id"] for s in client.get("/api/sources").json()}


def test_refresh_source(client, fakes):
    fakes.youtube.publish("UCdemoTech", "fresh000001", "Fresh")
    assert client.post("/api/sources/UCdemoTech/refresh").json() == {"new": 2}  # the demo feed already has one unseen video
    assert client.post("/api/sources/UCdemoTech/refresh").json() == {"new": 0}
    assert client.post("/api/sources/nope/refresh").status_code == 404


def test_refresh_source_youtube_error(client, fakes):
    fakes.youtube.feed_errors["UCdemoTech"] = models.YouTubeError("Sign in to confirm", bot_check=True)
    r = client.post("/api/sources/UCdemoTech/refresh")
    assert r.status_code == 502
    assert "Sign in to confirm" in r.json()["detail"] and "cookies" in r.json()["detail"]


def test_refresh_all(client, fakes):
    fakes.youtube.publish("UCdemoCooking", "fresh000002", "Fresh cooking")
    new = client.post("/api/refresh").json()["new"]
    assert new >= 1


# --- settings ----------------------------------------------------------------------


def test_settings_roundtrip(client):
    s = client.get("/api/settings").json()
    assert s["quality"] == "best" and "jellyfin_configured" in s and "library_path" in s
    r = client.put("/api/settings", json={"quality": "720p", "poll_interval_minutes": "30", "skip_live": "false"})
    assert r.status_code == 200
    assert r.json()["quality"] == "720p" and r.json()["poll_interval_minutes"] == "30"
    assert client.get("/api/settings").json()["skip_live"] == "false"


def test_settings_errors_name_the_key(client):
    r = client.put("/api/settings", json={"max_attempts": "0"})
    assert r.status_code == 400 and "max_attempts" in r.json()["detail"]
    r = client.put("/api/settings", json={"download_subfolder": "../x"})
    assert r.status_code == 400 and "download_subfolder" in r.json()["detail"]
    assert "nope" in client.put("/api/settings", json={"nope": "1"}).json()["detail"]
    assert client.put("/api/settings", json=["x"]).status_code == 422


# --- health / events / integrations ---------------------------------------------------


def test_health(client):
    assert_shape(client.get("/api/health").json(), contract.HealthJson)
    r = client.post("/api/health/check")
    assert r.status_code == 200
    assert_shape(r.json(), contract.HealthJson)
    assert r.json()["ytdlp"]["ok"] is True and r.json()["library"]["ok"] is True


def test_events(client):
    events = client.get("/api/events").json()
    assert events
    for e in events:
        assert_shape(e, contract.EventJson)
    assert len(client.get("/api/events", params={"limit": 1}).json()) == 1
    only = client.get("/api/events", params={"video_id": "demo_fail_1"}).json()
    assert only and {e["video_id"] for e in only} == {"demo_fail_1"}
    assert client.get("/api/events", params={"limit": 0}).status_code == 422


def test_jellyfin(client, fakes):
    assert client.post("/api/jellyfin/test").json() == {"ok": True, "detail": "fake"}
    r = client.post("/api/jellyfin/create-library")
    assert r.json()["ok"] is True and fakes.notifier.created == 1


def test_ytdlp_upgrade(client, fakes):
    r = client.post("/api/ytdlp/upgrade")
    assert r.status_code == 200
    assert set(r.json()) == {"version", "note"}
    assert fakes.youtube.upgraded_to is not None


# --- cookies -----------------------------------------------------------------------

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tabc\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\tLOGIN_INFO\tdef\n"
)


def test_cookies_lifecycle(client):
    c = client.get("/api/cookies").json()
    assert_shape(c, contract.CookiesJson)
    assert c["present"] is False
    r = client.post("/api/cookies", files={"file": ("cookies.txt", NETSCAPE.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    assert_shape(r.json(), contract.CookiesJson)
    assert r.json()["present"] is True and r.json()["cookie_count"] == 2
    assert client.get("/api/cookies").json()["present"] is True
    r = client.delete("/api/cookies")
    assert r.status_code == 200 and r.json()["present"] is False


def test_cookies_upload_errors(client):
    r = client.post("/api/cookies", files={"file": ("c.txt", b"this is not cookies", "text/plain")})
    assert r.status_code == 400
    big = b"#" * (1024 * 1024 + 1)
    r = client.post("/api/cookies", files={"file": ("c.txt", big, "text/plain")})
    assert r.status_code == 400 and "1 MB" in r.json()["detail"]
    assert client.post("/api/cookies").status_code == 422
