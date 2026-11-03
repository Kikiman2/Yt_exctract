"""Accounts (admin / user), login, session cookie, login rate limit and CSRF (Origin) check.

- Sessions: Starlette's signed-cookie SessionMiddleware (HttpOnly, SameSite=Lax,
  Secure when the request came in over HTTPS, 30 days; see AutoSecureSessionMiddleware).
- Users live in the database (scrypt password hashes). The first start turns the
  ADMIN_USERNAME / ADMIN_PASSWORD environment values into admin account #1; after
  that the database owns the password and the environment values are ignored.
- `require_login` is a router dependency on every page/API route except /login,
  /static and /healthz; it loads the user from the database on each request, so a
  disabled or deleted account loses access at once. `require_admin` additionally
  needs role=admin (403 JSON for the API, redirect to / for pages). API requests
  without a session get 401 JSON; pages are redirected to /login?next=<local path>.
- `csrf_middleware` rejects state-changing requests whose Origin (or Referer)
  doesn't match the Host / X-Forwarded-Host. The app sits behind
  nginx-proxy-manager, so this is the CSRF protection (plus SameSite=Lax).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections import deque
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import config, db

log = logging.getLogger(__name__)

SESSION_MAX_AGE = 30 * 24 * 3600
SESSION_COOKIE = "yt_extract_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

RATE_LIMIT_FAILURES = 5
RATE_LIMIT_WINDOW = 60.0

DEV_DEFAULT_USER = "admin"
DEV_DEFAULT_PASSWORD = "admin"


# --- configuration -----------------------------------------------------------


def check_config() -> None:
    """Called at startup: refuse to run without credentials and a session secret
    (unless DEV_FAKE=1)."""
    if config.DEV_FAKE:
        if not config.ADMIN_PASSWORD or not config.SESSION_SECRET:
            log.warning(
                "DEV_FAKE=1: using development credentials (%s / %s) and/or a random session secret",
                credentials()[0], DEV_DEFAULT_PASSWORD,
            )
        return
    missing = [name for name in ("ADMIN_PASSWORD", "SESSION_SECRET") if not getattr(config, name)]
    if missing:
        raise RuntimeError(f"Refusing to start: {', '.join(missing)} must be set (see .env.example)")


def session_secret() -> str:
    return config.SESSION_SECRET or secrets.token_urlsafe(32)


_request_is_https: ContextVar[bool] = ContextVar("request_is_https", default=False)


class HttpsAwareMiddleware:
    """Notes whether this request reached the app over HTTPS: directly, or through a TLS
    terminating proxy / Cloudflare tunnel that sends X-Forwarded-Proto: https. Lets the
    same instance serve plain http://LAN-ip:port and https://public-name with the right
    cookie flag on each. Faking the header only changes the Secure flag of the faker's
    own cookie."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            forwarded = headers.get(b"x-forwarded-proto", b"").split(b",")[0].strip().lower()
            _request_is_https.set(scope.get("scheme") in ("https", "wss") or forwarded == b"https")
        await self.app(scope, receive, send)


class AutoSecureSessionMiddleware(SessionMiddleware):
    """SessionMiddleware whose cookie gets `Secure` on HTTPS requests (always when
    COOKIE_SECURE=true). Starlette reads `security_flags` when it sets the cookie, so
    deciding per request is just a property."""

    @property
    def security_flags(self) -> str:
        if config.COOKIE_SECURE or _request_is_https.get():
            return self._flags if "secure" in self._flags else self._flags + "; secure"
        return self._flags

    @security_flags.setter
    def security_flags(self, value: str) -> None:
        self._flags = value


def session_middleware_kwargs() -> dict:
    return {
        "secret_key": session_secret(),
        "session_cookie": SESSION_COOKIE,
        "max_age": SESSION_MAX_AGE,
        "same_site": "lax",
        "https_only": False,  # decided per request by AutoSecureSessionMiddleware
    }


def credentials() -> tuple[str, str]:
    user = config.ADMIN_USERNAME or "admin"
    password = config.ADMIN_PASSWORD
    if not password and config.DEV_FAKE:
        password = DEV_DEFAULT_PASSWORD
    return user, password


# --- passwords ----------------------------------------------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
ROLES = ("admin", "user")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(expected), maxmem=128 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters")


_dummy_hash: str | None = None


async def bootstrap_admin() -> None:
    """Fresh database + ADMIN_PASSWORD set: create admin #1 from the environment.
    Then (every start, idempotent) give the admin the manual downloads that predate
    accounts. Safe to call repeatedly and from several requests at once."""
    if await db.count_users() == 0:
        name, password = credentials()
        if not password:
            return
        password_hash = await asyncio.to_thread(hash_password, password)
        if await db.create_user(name, password_hash, "admin") is not None:
            await db.add_event("info", "users", f"Created admin account '{name}' from the environment")
    admin_id = await db.first_admin_id()
    if admin_id is not None:
        await db.backfill_unowned_manual_videos(admin_id)


async def authenticate(username: str, password: str) -> dict | None:
    """The user (without hash) when the credentials are right and the account is enabled.
    A hash is always computed, so timing doesn't reveal whether the username exists."""
    global _dummy_hash
    await bootstrap_admin_if_empty()
    row = await db.get_user_by_name(username.strip()) if username else None
    if row is None:
        if _dummy_hash is None:
            _dummy_hash = await asyncio.to_thread(hash_password, "not-a-real-password")
        await asyncio.to_thread(verify_password, password, _dummy_hash)
        return None
    ok = await asyncio.to_thread(verify_password, password, row["password_hash"])
    if not ok or row["disabled"] or not password:
        return None
    return {k: v for k, v in row.items() if k != "password_hash"}


async def bootstrap_admin_if_empty() -> None:
    if await db.count_users() == 0:
        await bootstrap_admin()


# --- client ip / rate limit ---------------------------------------------------


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def client_ip(request: Request) -> str:
    """Peer address; X-Forwarded-For / X-Real-IP are honoured only when the peer is
    listed in TRUSTED_PROXIES (our reverse proxy). Trusting any private peer would
    let a LAN client rotate the header and dodge the login rate limit. The proxy
    appends the address it saw, so the right-most entry is the trustworthy one."""
    peer = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(peer):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return peer


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in config.TRUSTED_PROXIES:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


class LoginRateLimiter:
    def __init__(self, limit: int = RATE_LIMIT_FAILURES, window: float = RATE_LIMIT_WINDOW):
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._failures.setdefault(ip, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._prune(ip, time.monotonic())) >= self.limit

    def retry_after(self, ip: str) -> int:
        q = self._prune(ip, time.monotonic())
        if not q:
            return 0
        return max(1, int(self.window - (time.monotonic() - q[0])) + 1)

    def fail(self, ip: str) -> None:
        now = time.monotonic()
        self._prune(ip, now).append(now)
        if len(self._failures) > 10_000:  # bound memory under a spray of ips
            for key in [k for k, v in self._failures.items() if not v]:
                del self._failures[key]

    def reset(self, ip: str | None = None) -> None:
        if ip is None:
            self._failures.clear()
        else:
            self._failures.pop(ip, None)


rate_limiter = LoginRateLimiter()


# --- sessions -----------------------------------------------------------------


def login_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["user"] = user["username"]
    request.session["since"] = int(time.time())


def logout_session(request: Request) -> None:
    request.session.clear()


async def load_user(request: Request) -> dict | None:
    """The signed-in account from the database (None if logged out, disabled or deleted)."""
    if "session" not in request.scope:
        return None
    uid = request.session.get("uid")
    if not isinstance(uid, int):
        return None
    user = await db.get_user(uid)
    return user if user and not user["disabled"] else None


class NotAuthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


async def require_login(request: Request) -> dict:
    user = await load_user(request)
    if user is None:
        raise NotAuthenticated()
    request.state.user = user
    return user


async def require_admin(user: dict = Depends(require_login)) -> dict:
    if user["role"] != "admin":
        raise Forbidden()
    return user


def safe_next(value: str | None, default: str = "/") -> str:
    """Only allow local absolute paths ("/series/3?x=1"), never another host."""
    if not value or not isinstance(value, str):
        return default
    if any(ord(c) < 32 for c in value) or "\\" in value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    if value.startswith("/login") or value.startswith("/logout"):
        return default
    return value


async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    path = request.url.path
    if path.startswith("/api/") or path == "/api":
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(safe_next(target), safe='')}", status_code=303)


async def forbidden_handler(request: Request, exc: Forbidden):
    path = request.url.path
    if path.startswith("/api/") or path == "/api":
        return JSONResponse({"detail": "Administrator access required"}, status_code=403)
    return RedirectResponse("/", status_code=303)


# --- CSRF ---------------------------------------------------------------------


def _split_hostport(netloc: str) -> tuple[str, str | None]:
    netloc = netloc.strip().lower()
    if netloc.startswith("["):  # IPv6 literal
        end = netloc.find("]")
        host, rest = netloc[: end + 1], netloc[end + 1:]
        return host, rest[1:] if rest.startswith(":") else None
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        return host, port
    return netloc, None


def _same_host(origin_netloc: str, host_header: str) -> bool:
    if not origin_netloc or not host_header:
        return False
    oh, op = _split_hostport(origin_netloc)
    hh, hp = _split_hostport(host_header)
    if oh != hh:
        return False
    # A proxy may drop the port from Host; the hostname is what identifies us.
    return op is None or hp is None or op == hp


def origin_allowed(request: Request) -> bool:
    source = request.headers.get("origin")
    if not source or source == "null":
        source = request.headers.get("referer")
    if not source:
        return False
    netloc = urlsplit(source).netloc
    if not netloc:
        return False
    candidates = [request.headers.get("host", "")]
    fwd = request.headers.get("x-forwarded-host")
    if fwd:
        candidates.append(fwd.split(",")[0].strip())
    return any(_same_host(netloc, c) for c in candidates if c)


async def csrf_middleware(request: Request, call_next):
    if request.method.upper() not in SAFE_METHODS and not origin_allowed(request):
        return JSONResponse(
            {"detail": "Cross-site request blocked (Origin/Referer does not match Host)"},
            status_code=403,
        )
    return await call_next(request)
