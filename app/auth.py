"""Single-user login, session cookie, login rate limit and CSRF (Origin) check.

- Sessions: Starlette's signed-cookie SessionMiddleware (HttpOnly, SameSite=Lax,
  Secure when COOKIE_SECURE, 30 days).
- `require_login` is a router dependency on every page/API route except /login,
  /static and /healthz. API requests without a session get 401 JSON; pages are
  redirected to /login?next=<local path>.
- `csrf_middleware` rejects state-changing requests whose Origin (or Referer)
  doesn't match the Host / X-Forwarded-Host. The app sits behind
  nginx-proxy-manager, so this is the CSRF protection (plus SameSite=Lax).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
import time
from collections import deque
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import config

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


def session_middleware_kwargs() -> dict:
    return {
        "secret_key": session_secret(),
        "session_cookie": SESSION_COOKIE,
        "max_age": SESSION_MAX_AGE,
        "same_site": "lax",
        "https_only": config.COOKIE_SECURE,
    }


def credentials() -> tuple[str, str]:
    user = config.ADMIN_USERNAME or "admin"
    password = config.ADMIN_PASSWORD
    if not password and config.DEV_FAKE:
        password = DEV_DEFAULT_PASSWORD
    return user, password


def check_credentials(username: str, password: str) -> bool:
    user, expected = credentials()
    if not expected:
        return False
    # Evaluate both comparisons so timing doesn't reveal which one failed.
    ok_user = hmac.compare_digest(username.encode(), user.encode())
    ok_pass = hmac.compare_digest(password.encode(), expected.encode())
    return ok_user and ok_pass


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


def login_session(request: Request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username
    request.session["since"] = int(time.time())


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> str | None:
    user = request.session.get("user") if "session" in request.scope else None
    return user if isinstance(user, str) and user else None


class NotAuthenticated(Exception):
    pass


async def require_login(request: Request) -> str:
    user = current_user(request)
    if user is None:
        raise NotAuthenticated()
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
