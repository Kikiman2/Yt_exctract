"""Plain data types and exceptions shared by every layer. No I/O in here."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SourceKind = Literal["channel", "playlist"]

# videos.status values. pending -> downloading -> downloaded | skipped | failed;
# downloaded -> deleted (retention or manual); failed -> pending (retry).
VIDEO_STATUSES = ("pending", "downloading", "downloaded", "skipped", "failed", "deleted")


class YouTubeError(Exception):
    """yt-dlp / network failure. `bot_check` marks "Sign in to confirm you're not a bot"."""

    def __init__(self, message: str, bot_check: bool = False):
        super().__init__(message)
        self.bot_check = bot_check


class LibraryOffline(Exception):
    """The library sentinel file is missing (share not mounted / stale bind mount)."""


class CookiesInvalid(ValueError):
    """Uploaded cookies file is not a usable Netscape cookies.txt."""


@dataclass(frozen=True)
class SourceRef:
    """A subscribable thing: a channel (id UC...) or a playlist (id PL...)."""

    kind: SourceKind
    source_id: str
    title: str
    url: str


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    title: str
    channel_id: str | None = None
    channel_title: str | None = None


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    published: str | None = None  # ISO string from the feed, informational only


@dataclass
class VideoMeta:
    video_id: str
    title: str
    channel: str | None = None
    channel_id: str | None = None
    upload_date: str | None = None  # YYYY-MM-DD
    duration: int | None = None  # seconds
    description: str | None = None
    webpage_url: str | None = None


@dataclass
class DownloadRequest:
    video_id: str
    url: str
    staging_dir: Path  # yt-dlp writes <video_id>.<ext> files in here
    quality: str  # key of config.QUALITY_FORMATS
    skip_shorts: bool
    shorts_max_seconds: int
    skip_live: bool
    force: bool  # manual link: bypass shorts/live filters


@dataclass
class DownloadResult:
    """`video` is None when skip_reason is set (filtered out, nothing downloaded)."""

    video: Path | None
    thumbnail: Path | None = None  # jpg/webp next to the video, if written
    meta: VideoMeta | None = None
    skip_reason: str | None = None


@dataclass
class InstallResult:
    rel_path: str  # path relative to the library root, forward slashes
    size: int


@dataclass
class CookiesStatus:
    present: bool
    cookie_count: int = 0
    youtube_cookie_count: int = 0
    expires_at: str | None = None  # earliest expiry among login cookies, ISO date
    modified_at: str | None = None
    detail: str = ""


@dataclass
class ProgressInfo:
    status: Literal["downloading", "processing"]
    percent: float | None = None
    speed: float | None = None
    eta: int | None = None


@dataclass
class HealthItem:
    ok: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)
