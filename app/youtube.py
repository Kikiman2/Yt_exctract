import re

import feedparser
import yt_dlp

from . import config

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _normalize_channel_input(url_or_handle: str) -> str:
    value = url_or_handle.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if not value.startswith("@"):
        value = "@" + value
    return f"https://www.youtube.com/{value}"


def resolve_channel(url_or_handle: str) -> tuple[str, str, str]:
    """Resolve a channel URL/handle to (channel_id, title, canonical_url).

    Runs a blocking network call - call via asyncio.to_thread from async code.
    """
    url = _normalize_channel_input(url_or_handle)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        **config.youtube_ydl_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    channel_id = info.get("channel_id") or info.get("id")
    title = info.get("channel") or info.get("title") or info.get("uploader") or channel_id

    if not channel_id or not re.match(r"^UC[\w-]{22}$", channel_id):
        raise ValueError(f"Could not resolve a channel id from '{url_or_handle}'")

    return channel_id, title, url


def resolve_video(url: str) -> tuple[str, str]:
    """Resolve a video URL to (video_id, title).

    Runs a blocking network call - call via asyncio.to_thread from async code.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, **config.youtube_ydl_opts()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    video_id = info.get("id")
    title = info.get("title") or video_id
    if not video_id:
        raise ValueError(f"Could not resolve a video id from '{url}'")
    return video_id, title


def fetch_latest_videos(channel_id: str) -> list[tuple[str, str]]:
    """Fetch the channel's RSS feed and return [(video_id, title), ...].

    Runs a blocking network call - call via asyncio.to_thread from async code.
    """
    feed = feedparser.parse(RSS_URL.format(channel_id=channel_id))
    results = []
    for entry in feed.entries:
        video_id = entry.get("yt_videoid")
        title = entry.get("title")
        if video_id and title:
            results.append((video_id, title))
    return results
