"""Upload / inspect / delete the yt-dlp cookies file (`config.COOKIES_FILE`)."""

from __future__ import annotations

import asyncio
import dataclasses
import os

from .. import config, db
from ..youtube import cookies as yt_cookies


def _status() -> dict:
    return dataclasses.asdict(yt_cookies.inspect(config.COOKIES_FILE))


async def status() -> dict:
    """CookiesJson, read live from disk (cheap, so no cache)."""
    return await asyncio.to_thread(_status)


def _write(text: str) -> None:
    path = config.COOKIES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temp file + rename so a concurrent download never reads a half-written file.
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


async def save(data: bytes) -> dict:
    """Accepts Netscape cookies.txt or the JSON export formats. Raises
    models.CookiesInvalid (a ValueError) for anything unusable; the old file stays."""
    text = yt_cookies.parse_cookies(data)
    await asyncio.to_thread(_write, text)
    result = await status()
    await db.add_event("info", "cookies", f"Cookies uploaded ({result['cookie_count']} cookies)")
    return result


async def delete() -> dict:
    await asyncio.to_thread(config.COOKIES_FILE.unlink, True)
    await db.add_event("info", "cookies", "Cookies deleted")
    return await status()
