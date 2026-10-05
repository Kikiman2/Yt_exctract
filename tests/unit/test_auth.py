from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import auth, config
from app.main import create_app

BASE = "http://testserver"
ORIGIN = {"Origin": BASE}


@pytest.fixture
def app(monkeypatch, services_ctx):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret-pass")
    monkeypatch.setattr(config, "SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DEV_FAKE", False)
    auth.rate_limiter.reset()
    yield create_app()
    auth.rate_limiter.reset()


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE, follow_redirects=False)


def login(client, password="s3cret-pass", username="admin", next_="/", headers=ORIGIN):
    return client.post("/login", data={"username": username, "password": password, "next": next_}, headers=headers)


def test_login_ok_sets_cookie_and_redirects(client):
    r = login(client, next_="/sources")
    assert r.status_code == 303
    assert r.headers["location"] == "/sources"
    cookie = r.headers["set-cookie"].lower()
    assert "yt_extract_session=" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=2592000" in cookie
    assert client.get("/sources").status_code == 200


def test_secure_cookie_flag(monkeypatch, app):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    c = TestClient(create_app(), base_url=BASE, follow_redirects=False)
    assert "secure" in login(c).headers["set-cookie"].lower()


def test_login_bad_password(client):
    r = login(client, password="wrong")
    assert r.status_code == 401
    assert "Wrong username or password" in r.text
    assert client.get("/").status_code == 303


def test_login_bad_username(client):
    assert login(client, username="root").status_code == 401


def test_login_rate_limit(client):
    for _ in range(5):
        assert login(client, password="nope").status_code == 401
    r = login(client)  # even the right password is refused while blocked
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "Too many failed attempts" in r.text


def test_rate_limit_is_per_ip():
    limiter = auth.LoginRateLimiter(limit=2, window=60)
    limiter.fail("1.1.1.1")
    limiter.fail("1.1.1.1")
    assert limiter.blocked("1.1.1.1")
    assert not limiter.blocked("2.2.2.2")


def _request(peer, headers):
    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": (peer, 1234), "query_string": b""}
    return Request(scope)


def test_client_ip_honours_forwarded_for_only_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(auth.config, "TRUSTED_PROXIES", ["172.16.0.0/12", "127.0.0.1"])
    # an untrusted LAN client can't choose its own rate-limit bucket
    assert auth.client_ip(_request("192.168.1.50", {"X-Forwarded-For": "1.2.3.4"})) == "192.168.1.50"
    assert auth.client_ip(_request("172.17.0.5", {"X-Forwarded-For": "8.8.8.8, 203.0.113.9"})) == "203.0.113.9"
    assert auth.client_ip(_request("127.0.0.1", {"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"
    assert auth.client_ip(_request("8.8.4.4", {"X-Forwarded-For": "10.0.0.1"})) == "8.8.4.4"


@pytest.mark.parametrize("path", ["/", "/sources", "/settings", "/health"])
def test_protected_pages_redirect_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/login"
    assert parse_qs(loc.query)["next"] == [path]


@pytest.mark.parametrize("path", ["/api/status", "/api/videos", "/api/sources", "/api/settings",
                                  "/api/health", "/api/events", "/api/cookies"])
def test_api_requires_session(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json() == {"detail": "Not logged in"}


def test_api_write_requires_session(client):
    assert client.post("/api/queue/pause", headers=ORIGIN).status_code == 401


def test_public_paths(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/login").status_code == 200
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_docs_disabled(client):
    login(client)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_origin_check_blocks_cross_site_posts(client):
    login(client)
    assert client.post("/api/queue/pause").status_code == 403  # no Origin, no Referer
    assert client.post("/api/queue/pause", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/queue/pause", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/queue/pause", headers={"Referer": "https://evil.example/x"}).status_code == 403
    assert client.post("/api/queue/pause", headers={"Referer": f"{BASE}/"}).status_code == 200
    assert client.post("/api/queue/pause", headers=ORIGIN).status_code == 200
    assert client.patch("/api/sources/x", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.delete("/api/sources/x", headers={"Origin": "https://evil.example"}).status_code == 403


def test_origin_check_on_login_and_logout(client):
    assert login(client, headers={"Origin": "https://evil.example"}).status_code == 403
    login(client)
    assert client.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/").status_code == 200


def test_origin_check_behind_proxy(client):
    login(client)
    r = client.post("/api/queue/pause", headers={"Origin": "https://yt.example.com",
                                                 "X-Forwarded-Host": "yt.example.com"})
    assert r.status_code == 200
    r = client.post("/api/queue/pause", headers={"Origin": "https://evil.example",
                                                 "X-Forwarded-Host": "yt.example.com"})
    assert r.status_code == 403


def test_same_host_port_rules():
    assert auth._same_host("example.com:2000", "example.com:2000")
    assert auth._same_host("example.com", "example.com:443")
    assert not auth._same_host("example.com:2001", "example.com:2000")
    assert not auth._same_host("example.com.evil.net", "example.com")
    assert auth._same_host("[::1]:2000", "[::1]:2000")


def test_logout_requires_post_and_clears_session(client):
    login(client)
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code in (404, 405)
    r = client.post("/logout", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 303
    assert client.get("/api/status").status_code == 401


@pytest.mark.parametrize("bad", [
    "https://evil.example", "//evil.example", "/\\evil.example", "\\\\evil.example", "http:/evil.example",
    "javascript:alert(1)", "/ok\r\nLocation: https://evil.example", "evil.example", "",
])
def test_next_param_open_redirect_prevented(client, bad):
    r = login(client, next_=bad)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_page_next_is_sanitised(client):
    r = client.get("/login", params={"next": "//evil.example"})
    assert 'name="next" value="/"' in r.text
    r = client.get("/login", params={"next": "/sources?x=1"})
    assert 'value="/sources?x=1"' in r.text


def test_logged_in_login_page_redirects_to_next(client):
    login(client)
    r = client.get("/login", params={"next": "/settings"})
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    r = client.get("/login", params={"next": "https://evil.example"})
    assert r.headers["location"] == "/"


def test_safe_next():
    assert auth.safe_next("/sources?a=b#c") == "/sources?a=b#c"
    assert auth.safe_next("/login?next=/x") == "/"
    assert auth.safe_next(None) == "/"


def test_check_config_refuses_missing_secrets(monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "SESSION_SECRET", "x")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        auth.check_config()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "x")
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        auth.check_config()
    monkeypatch.setattr(config, "DEV_FAKE", True)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    auth.check_config()  # dev mode is allowed, with admin/admin
    assert auth.check_credentials("admin", "admin")


def test_startup_refused_without_secrets(monkeypatch, app):
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_empty_password_never_matches(monkeypatch):
    monkeypatch.setattr(config, "DEV_FAKE", False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert not auth.check_credentials("admin", "")
