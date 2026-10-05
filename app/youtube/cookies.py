"""Cookies upload handling: normalise to Netscape cookies.txt and report on the file.

Why: yt-dlp only reads Netscape format, but browser extensions often export JSON (see the
old scripts/convert_cookies.py, whose shapes are handled here). The status helps the UI
warn when the login cookies are missing or about to expire."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import CookiesInvalid, CookiesStatus

HEADER = "# Netscape HTTP Cookie File"
FAR_FUTURE_EXPIRY = 2145916800  # 2038-01-01, for session cookies without an expiry
LOGIN_COOKIES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
})
_HTTPONLY_PREFIX = "#HttpOnly_"


def _get(cookie: dict, *keys, default=None):
    for key in keys:
        if key in cookie and cookie[key] is not None:
            return cookie[key]
    return default


def _json_to_netscape(cookies: list) -> str:
    lines = [HEADER, ""]
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = _get(cookie, "domain")
        name = _get(cookie, "name")
        value = _get(cookie, "value")
        if not domain or not name or value is None:
            continue
        host_only = _get(cookie, "hostOnly")
        if host_only is None:
            include_subdomains = domain.startswith(".")
        else:
            include_subdomains = not host_only
            if include_subdomains and not domain.startswith("."):
                domain = "." + domain
        try:
            expiry = int(float(_get(cookie, "expirationDate", "expires", "expiry", default=0)))
        except (TypeError, ValueError):
            expiry = 0
        if expiry <= 0:
            expiry = FAR_FUTURE_EXPIRY
        lines.append("\t".join([
            domain,
            "TRUE" if include_subdomains else "FALSE",
            _get(cookie, "path", default="/"),
            "TRUE" if _get(cookie, "secure", default=False) else "FALSE",
            str(expiry),
            str(name),
            str(value),
        ]))
    return "\n".join(lines) + "\n"


def _netscape_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if line.startswith(_HTTPONLY_PREFIX):
            line = line[len(_HTTPONLY_PREFIX):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7 and fields[0] and fields[5]:
            rows.append(fields)
    return rows


def _from_json(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CookiesInvalid("Not a Netscape cookies.txt and not valid JSON") from exc
    # Wrapped exports: {"cookies": [...]} (Cookie-Editor variants, Playwright storage state).
    if isinstance(data, dict):
        data = data.get("cookies")
    if not isinstance(data, list):
        raise CookiesInvalid("JSON cookie export has no cookie list")
    youtube = [c for c in data if isinstance(c, dict) and "youtube.com" in str(_get(c, "domain", default=""))]
    converted = _json_to_netscape(youtube or data)
    if not _netscape_rows(converted):
        raise CookiesInvalid("No usable cookies found in the JSON export")
    return converted


def parse_cookies(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookiesInvalid("Cookies file is not UTF-8 text") from exc
    if not text.strip():
        raise CookiesInvalid("Cookies file is empty")
    if text.lstrip().startswith(("[", "{")):
        return _from_json(text)
    rows = _netscape_rows(text)
    if not rows:
        raise CookiesInvalid("No cookies found: expected a Netscape cookies.txt (7 tab-separated fields per line)")
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def _iso_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


def inspect(path: Path) -> CookiesStatus:
    if not path.is_file():
        return CookiesStatus(present=False, detail="No cookies file uploaded")
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
    modified_at = modified.isoformat(timespec="seconds")
    try:
        rows = _netscape_rows(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return CookiesStatus(present=True, modified_at=modified_at, detail="Cookies file is unreadable")
    youtube = [r for r in rows if "youtube.com" in r[0]]
    login = [r for r in rows if r[5] in LOGIN_COOKIES and ("youtube.com" in r[0] or "google." in r[0])]
    expiries = []
    for row in login:
        try:
            expiry = int(float(row[4]))
        except ValueError:
            continue
        if expiry > 0:
            expiries.append(expiry)
    if not rows:
        detail = "Cookies file contains no cookies"
    elif not login:
        detail = "No YouTube login cookies (SID, __Secure-3PSID, LOGIN_INFO) found; not signed in"
    else:
        detail = f"{len(login)} login cookie(s) present"
    return CookiesStatus(
        present=True,
        cookie_count=len(rows),
        youtube_cookie_count=len(youtube),
        expires_at=_iso_date(min(expiries)) if expiries else None,
        modified_at=modified_at,
        detail=detail,
    )
