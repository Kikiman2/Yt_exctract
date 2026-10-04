"""Turn whatever the user pasted into something yt-dlp understands. Pure.

Why: the add-subscription box accepts channel URLs, @handles, bare names and playlist
links; the add-video box accepts every YouTube video URL flavour."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_VIDEO_ID = re.compile(r"^[\w-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_HANDLE = re.compile(r"^@?[\w.\-]+$")
# Auto-generated mixes ("RD...") can't be listed as a feed.
_UNSUPPORTED_LIST_PREFIXES = ("RD",)
_VIDEO_PATH_PREFIXES = ("shorts", "live", "embed", "v")


def _is_youtube(host: str) -> bool:
    host = host.lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def classify_input(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Enter a channel URL, @handle or playlist URL")

    if re.match(r"^(https?://|(www\.|m\.)?youtube\.com/)", raw, re.IGNORECASE):
        parsed = urlparse(raw if "//" in raw else "https://" + raw)
        if not _is_youtube(parsed.netloc):
            raise ValueError("Only YouTube URLs are supported")
        query = parse_qs(parsed.query)
        list_id = (query.get("list") or [""])[0]
        if list_id or parsed.path.rstrip("/") == "/playlist":
            if not list_id:
                raise ValueError("Playlist URL has no list= parameter")
            if list_id.startswith(_UNSUPPORTED_LIST_PREFIXES):
                raise ValueError("Auto-generated mix playlists can't be subscribed to")
            return "playlist_url", f"https://www.youtube.com/playlist?list={list_id}"
        if extract_video_id(raw):
            raise ValueError("That is a video link; add it as a video, or give a channel URL")
        path = parsed.path.rstrip("/")
        if not path:
            raise ValueError("URL does not point at a channel or playlist")
        return "channel_url", f"https://www.youtube.com{path}"

    if _CHANNEL_ID.match(raw):
        return "channel_url", f"https://www.youtube.com/channel/{raw}"
    if not _HANDLE.match(raw):
        raise ValueError(f"'{raw}' is not a channel URL, handle or playlist URL")
    name = raw.lstrip("@")
    if not name:
        raise ValueError("Handle is empty")
    return "handle", f"https://www.youtube.com/@{name}"


def extract_video_id(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "//" in raw else "https://" + raw)
    if not _is_youtube(parsed.netloc):
        return None
    host = parsed.netloc.lower()
    segments = [s for s in parsed.path.split("/") if s]
    candidate: str | None = None
    if host.split(":")[0] == "youtu.be":
        candidate = segments[0] if segments else None
    elif segments and segments[0] == "watch":
        candidate = (parse_qs(parsed.query).get("v") or [None])[0]
    elif len(segments) >= 2 and segments[0] in _VIDEO_PATH_PREFIXES:
        candidate = segments[1]
    return candidate if candidate and _VIDEO_ID.match(candidate) else None
