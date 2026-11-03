"""Accounts: password hashing, admin bootstrap, role checks on every admin route,
per-user video lists, shared downloads, and the user-management rules."""

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db
from app.main import create_app

BASE = "http://testserver"
ORIGIN = {"Origin": BASE}
VIDEO_URL = "https://www.youtube.com/watch?v={}"


@pytest.fixture
def app(monkeypatch, services_ctx):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret-pass")
    monkeypatch.setattr(config, "SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DEV_FAKE", False)
    auth.rate_limiter.reset()
    yield create_app()
    auth.rate_limiter.reset()


def new_client(app) -> TestClient:
    return TestClient(app, base_url=BASE, follow_redirects=False, headers=ORIGIN)


def sign_in(client, username, password):
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303, r.text
    return client


@pytest.fixture
def admin(app):
    return sign_in(new_client(app), "admin", "s3cret-pass")


@pytest.fixture
def make_user(app, admin):
    def make(name="alice", password="alice-pass1", role="user"):
        r = admin.post("/api/users", json={"username": name, "password": password, "role": role})
        assert r.status_code == 200, r.text
        return sign_in(new_client(app), name, password)
    return make


@pytest.fixture
def alice(make_user):
    return make_user("alice")


def video(fake_youtube, video_id, title="T", channel=None):
    fake_youtube.add_video(video_id, title, channel)
    return VIDEO_URL.format(video_id)


# --- passwords ---------------------------------------------------------------------


def test_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert auth.hash_password("correct horse") != h  # fresh salt each time


@pytest.mark.parametrize("stored", ["", "garbage", "md5$1$2$3$a$b", "scrypt$x$y$z$!!$!!"])
def test_verify_rejects_malformed_hashes(stored):
    assert not auth.verify_password("anything", stored)


# --- bootstrap ---------------------------------------------------------------------


async def test_first_login_creates_admin_from_env(app, database):
    assert await database.count_users() == 0
    client = sign_in(new_client(app), "admin", "s3cret-pass")
    assert await database.count_users() == 1
    assert client.get("/api/me").json() == {"id": 1, "username": "admin", "role": "admin"}


async def test_database_owns_password_after_bootstrap(app, database, monkeypatch):
    sign_in(new_client(app), "admin", "s3cret-pass")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "changed-in-env")
    r = new_client(app).post("/login", data={"username": "admin", "password": "changed-in-env"})
    assert r.status_code == 401
    sign_in(new_client(app), "admin", "s3cret-pass")


async def test_bootstrap_gives_legacy_manual_videos_to_admin(database, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    await database.add_video("legacy00001", None, "Old manual", force=True)
    await database.add_subscription("UC1", "channel", "Sub", "u")
    await database.add_video("subvid00001", "UC1", "From a sub")
    await auth.bootstrap_admin()
    admin_id = await database.first_admin_id()
    assert await database.has_request(admin_id, "legacy00001")
    assert not await database.has_request(admin_id, "subvid00001")
    await auth.bootstrap_admin()  # idempotent
    assert await database.count_users() == 1


# --- permissions -------------------------------------------------------------------

ADMIN_API = [
    ("get", "/api/sources"), ("post", "/api/sources"), ("patch", "/api/sources/x"),
    ("delete", "/api/sources/x"), ("post", "/api/sources/x/refresh"), ("post", "/api/refresh"),
    ("get", "/api/settings"), ("put", "/api/settings"), ("get", "/api/health"),
    ("post", "/api/health/check"), ("get", "/api/events"), ("post", "/api/jellyfin/test"),
    ("post", "/api/jellyfin/create-library"), ("post", "/api/ytdlp/upgrade"),
    ("get", "/api/cookies"), ("post", "/api/cookies"), ("delete", "/api/cookies"),
    ("post", "/api/queue/pause"), ("post", "/api/queue/resume"),
    ("post", "/api/videos/check-missing"),
    ("get", "/api/users"), ("post", "/api/users"), ("patch", "/api/users/1"), ("delete", "/api/users/1"),
]


@pytest.mark.parametrize("method,path", ADMIN_API)
def test_user_gets_403_on_every_admin_route(alice, method, path):
    r = getattr(alice, method)(path, **({"json": {}} if method in ("post", "put", "patch") and path != "/api/cookies" else {}))
    assert r.status_code == 403, (method, path, r.status_code)
    assert r.json() == {"detail": "Administrator access required"}


@pytest.mark.parametrize("path", ["/sources", "/settings", "/health", "/users"])
def test_user_is_sent_home_from_admin_pages(alice, admin, path):
    r = alice.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert admin.get(path).status_code == 200


def test_user_pages(alice, admin):
    home = alice.get("/")
    assert home.status_code == 200 and "My downloads" in home.text
    assert "Sources" not in home.text and "Settings" not in home.text
    assert 'data-role="user"' in home.text
    assert alice.get("/account").status_code == 200
    assert "Dashboard" in admin.get("/").text and 'data-role="admin"' in admin.get("/").text


def test_user_status_hides_health_details(alice, admin):
    assert set(alice.get("/api/status").json()) == {"queue"}
    assert set(alice.get("/api/status").json()["queue"]) == {"paused", "pause_reasons"}
    assert set(admin.get("/api/status").json()) == {"health", "queue"}


def test_unauthenticated_users_api_is_401(app):
    assert new_client(app).get("/api/users").status_code == 401


# --- per-user videos ---------------------------------------------------------------


def test_users_only_see_their_own_videos(admin, alice, make_user, fake_youtube):
    bob = make_user("bob", "bob-pass12")
    assert alice.post("/api/videos", json={"url": video(fake_youtube, "alicevid001")}).status_code == 200
    assert bob.post("/api/videos", json={"url": video(fake_youtube, "bobvid00001")}).status_code == 200
    assert admin.post("/api/videos", json={"url": video(fake_youtube, "adminvid001")}).status_code == 200
    ids = lambda c: {v["video_id"] for v in c.get("/api/videos").json()}  # noqa: E731
    assert ids(alice) == {"alicevid001"}
    assert ids(bob) == {"bobvid00001"}
    assert ids(admin) == {"alicevid001", "bobvid00001", "adminvid001"}


def test_user_cannot_touch_other_peoples_videos(admin, alice, fake_youtube):
    admin.post("/api/videos", json={"url": video(fake_youtube, "adminvid001")})
    assert alice.post("/api/videos/adminvid001/retry", json={}).status_code == 404
    assert alice.post("/api/videos/adminvid001/delete", json={}).status_code == 404
    assert alice.post("/api/videos/nope/delete", json={}).status_code == 404


def test_user_source_id_filter_cannot_widen_the_list(admin, alice, fake_youtube):
    admin.post("/api/videos", json={"url": video(fake_youtube, "adminvid001")})
    assert alice.get("/api/videos", params={"source_id": "anything"}).json() == []


async def test_shared_video_is_downloaded_once(admin, alice, fake_youtube, database):
    url = video(fake_youtube, "sharedvid01")
    admin.post("/api/videos", json={"url": url})
    alice.post("/api/videos", json={"url": url})
    assert len(await database.list_videos()) == 1
    assert await database.count_requests("sharedvid01") == 2
    assert [v["video_id"] for v in alice.get("/api/videos").json()] == ["sharedvid01"]


async def test_user_delete_keeps_file_while_others_hold_it(admin, alice, fake_youtube, database):
    url = video(fake_youtube, "sharedvid01")
    admin.post("/api/videos", json={"url": url})
    alice.post("/api/videos", json={"url": url})
    await database.update_video("sharedvid01", {"status": "downloaded", "file_path": "a/b.mp4"})
    r = alice.post("/api/videos/sharedvid01/delete", json={})
    assert r.status_code == 200
    assert alice.get("/api/videos").json() == []
    row = await database.get_video("sharedvid01")
    assert row["status"] == "downloaded" and row["file_path"] == "a/b.mp4"
    assert [v["video_id"] for v in admin.get("/api/videos").json()] == ["sharedvid01"]


async def test_user_delete_removes_file_when_sole_holder(alice, fake_youtube, database, services_ctx):
    alice.post("/api/videos", json={"url": video(fake_youtube, "alonevid001")})
    services_ctx.writer.write_bytes("Folder", "alonevid001.mp4", b"x")
    await database.update_video("alonevid001", {"status": "downloaded", "file_path": "Folder/alonevid001.mp4"})
    r = alice.post("/api/videos/alonevid001/delete", json={})
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert not services_ctx.writer.exists("Folder/alonevid001.mp4")


async def test_user_delete_never_removes_subscription_video(admin, alice, fake_youtube, database):
    chan = fake_youtube.add_channel("UCchan", "Chan")
    await database.add_subscription("UCchan", "channel", "Chan", chan.url)
    await database.add_video("subvid00001", "UCchan", "From the feed")
    await database.update_video("subvid00001", {"status": "downloaded", "file_path": "Chan/x.mp4"})
    alice.post("/api/videos", json={"url": video(fake_youtube, "subvid00001", channel=chan)})
    assert [v["video_id"] for v in alice.get("/api/videos").json()] == ["subvid00001"]
    alice.post("/api/videos/subvid00001/delete", json={})
    assert (await database.get_video("subvid00001"))["status"] == "downloaded"


async def test_unowned_legacy_video_is_not_deletable_by_a_user(admin, alice, fake_youtube, database):
    # an older manual download with no holder: a user asking for it must not become its only owner
    await database.add_video("legacy00001", None, "Old manual", force=True)
    await database.update_video("legacy00001", {"status": "downloaded", "file_path": "M/x.mp4"})
    alice.post("/api/videos", json={"url": video(fake_youtube, "legacy00001")})
    alice.post("/api/videos/legacy00001/delete", json={})
    assert (await database.get_video("legacy00001"))["status"] == "downloaded"
    assert await database.has_request(await database.first_admin_id(), "legacy00001")


def test_user_playlist_requests_are_recorded(alice, fake_youtube):
    from app.models import VideoRef
    fake_youtube.playlists["https://www.youtube.com/playlist?list=PLabcdefgh12"] = (
        "Mix", [VideoRef("pl_vid_0001", "One", None, "Up"), VideoRef("pl_vid_0002", "Two", None, "Up")])
    r = alice.post("/api/videos", json={"url": "https://www.youtube.com/playlist?list=PLabcdefgh12"})
    assert r.status_code == 200 and r.json()["queued"] == 2
    assert {v["video_id"] for v in alice.get("/api/videos").json()} == {"pl_vid_0001", "pl_vid_0002"}


# --- user management ---------------------------------------------------------------


def test_create_user_validation(admin, alice):
    post = lambda **kw: admin.post("/api/users", json=kw)  # noqa: E731
    assert post(username="ab", password="long-enough").status_code == 400
    assert post(username="has space", password="long-enough").status_code == 400
    assert post(username="carol", password="short").status_code == 400
    assert post(username="carol", password="long-enough", role="root").status_code == 400
    assert post(username="ALICE", password="long-enough").status_code == 400  # case-insensitive


def test_listing_never_exposes_hashes(admin, alice):
    users = admin.get("/api/users").json()
    assert {u["username"] for u in users} == {"admin", "alice"}
    assert all("password" not in k for u in users for k in u)


def test_login_is_case_insensitive_on_username(app, alice):
    sign_in(new_client(app), "ALICE", "alice-pass1")


def test_wrong_password_and_unknown_user_look_the_same(app, alice):
    a = new_client(app).post("/login", data={"username": "alice", "password": "nope-nope"})
    b = new_client(app).post("/login", data={"username": "nobody", "password": "nope-nope"})
    assert a.status_code == b.status_code == 401
    assert "Wrong username or password" in a.text and "Wrong username or password" in b.text


def test_disabled_user_loses_access_immediately(admin, alice, app):
    uid = alice.get("/api/me").json()["id"]
    assert admin.patch(f"/api/users/{uid}", json={"disabled": True}).status_code == 200
    assert alice.get("/api/me").status_code == 401
    assert new_client(app).post("/login", data={"username": "alice", "password": "alice-pass1"}).status_code == 401
    admin.patch(f"/api/users/{uid}", json={"disabled": False})
    sign_in(new_client(app), "alice", "alice-pass1")


def test_deleted_user_loses_access_and_videos_stay(admin, alice, fake_youtube):
    alice.post("/api/videos", json={"url": video(fake_youtube, "alicevid001")})
    uid = alice.get("/api/me").json()["id"]
    assert admin.delete(f"/api/users/{uid}").status_code == 200
    assert alice.get("/api/me").status_code == 401
    assert [v["video_id"] for v in admin.get("/api/videos").json()] == ["alicevid001"]


def test_cannot_lock_yourself_out(admin):
    me = admin.get("/api/me").json()["id"]
    assert admin.delete(f"/api/users/{me}").status_code == 400
    assert admin.patch(f"/api/users/{me}", json={"role": "user"}).status_code == 400
    assert admin.patch(f"/api/users/{me}", json={"disabled": True}).status_code == 400
    assert admin.get("/api/users").status_code == 200


def test_role_change_and_second_admin(admin, alice, app):
    uid = alice.get("/api/me").json()["id"]
    assert admin.patch(f"/api/users/{uid}", json={"role": "admin"}).json()["role"] == "admin"
    assert alice.get("/api/users").status_code == 200  # role is read on every request
    assert alice.patch("/api/users/1", json={"role": "user"}).status_code == 200  # demote the original
    assert admin.get("/api/users").status_code == 403


def test_unknown_user_and_fields(admin):
    assert admin.patch("/api/users/999", json={"role": "user"}).status_code == 404
    assert admin.delete("/api/users/999").status_code == 404
    assert admin.patch("/api/users/1", json={"nonsense": 1}).status_code == 400


def test_admin_resets_a_password(admin, alice, app):
    uid = alice.get("/api/me").json()["id"]
    assert admin.patch(f"/api/users/{uid}", json={"password": "short"}).status_code == 400
    assert admin.patch(f"/api/users/{uid}", json={"password": "brand-new-pass"}).status_code == 200
    assert new_client(app).post("/login", data={"username": "alice", "password": "alice-pass1"}).status_code == 401
    sign_in(new_client(app), "alice", "brand-new-pass")


def test_change_own_password(alice, app):
    r = alice.post("/api/me/password", json={"current_password": "wrong", "new_password": "another-pass1"})
    assert r.status_code == 400 and "Current password" in r.json()["detail"]
    r = alice.post("/api/me/password", json={"current_password": "alice-pass1", "new_password": "short"})
    assert r.status_code == 400
    r = alice.post("/api/me/password", json={"current_password": "alice-pass1", "new_password": "another-pass1"})
    assert r.status_code == 200
    sign_in(new_client(app), "alice", "another-pass1")
    assert new_client(app).post("/login", data={"username": "alice", "password": "alice-pass1"}).status_code == 401


def test_csrf_still_applies_to_user_routes(app, alice):
    c = TestClient(app, base_url=BASE, follow_redirects=False, cookies=alice.cookies)
    assert c.post("/api/me/password", json={}).status_code == 403  # no Origin header
