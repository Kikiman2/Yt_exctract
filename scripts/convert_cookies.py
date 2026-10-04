#!/usr/bin/env python3
"""Convert a browser extension's JSON cookie export into Netscape cookies.txt
format, which is what yt-dlp's --cookies / cookiefile option expects.

Handles the common JSON shapes exported by extensions like "Get cookies.txt
LOCALLY" (JSON mode), "Cookie-Editor", "EditThisCookie", and Playwright/
Puppeteer storage-state dumps.

Usage:
    python3 scripts/convert_cookies.py exported.json data/cookies.txt
"""

import json
import sys

FAR_FUTURE_EXPIRY = 2145916800  # 2038-01-01, used for session/no-expiry cookies


def _get(cookie: dict, *keys, default=None):
    for key in keys:
        if key in cookie and cookie[key] is not None:
            return cookie[key]
    return default


def convert(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in cookies:
        domain = _get(cookie, "domain")
        name = _get(cookie, "name")
        value = _get(cookie, "value")
        if not domain or not name or value is None:
            continue

        path = _get(cookie, "path", default="/")
        secure = bool(_get(cookie, "secure", default=False))
        host_only = _get(cookie, "hostOnly")
        if host_only is None:
            include_subdomains = domain.startswith(".")
        else:
            include_subdomains = not host_only
            if include_subdomains and not domain.startswith("."):
                domain = "." + domain

        expiry = _get(cookie, "expirationDate", "expires", "expiry", default=0)
        try:
            expiry = int(float(expiry))
        except (TypeError, ValueError):
            expiry = 0
        if expiry <= 0:
            expiry = FAR_FUTURE_EXPIRY

        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if include_subdomains else "FALSE",
                    path,
                    "TRUE" if secure else "FALSE",
                    str(expiry),
                    name,
                    str(value),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.json> <output cookies.txt>", file=sys.stderr)
        sys.exit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # Some exports wrap the list under a key (e.g. {"cookies": [...]})
    if isinstance(data, dict):
        data = data.get("cookies", [])

    youtube_cookies = [c for c in data if "youtube.com" in _get(c, "domain", default="")]
    if not youtube_cookies:
        print("Warning: no youtube.com cookies found in input, converting all cookies", file=sys.stderr)
        youtube_cookies = data

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(convert(youtube_cookies))

    print(f"Wrote {len(youtube_cookies)} cookie(s) to {output_path}")


if __name__ == "__main__":
    main()
