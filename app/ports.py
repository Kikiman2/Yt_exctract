"""Interfaces between the orchestration layer (services/) and the outside world.
Real implementations: youtube/client.py, library/writer.py, library/jellyfin.py.
Test doubles: tests/fakes.py (also wired in by DEV_FAKE=1)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from .models import (
    DownloadRequest,
    DownloadResult,
    FeedEntry,
    InstallResult,
    ProgressInfo,
    SourceRef,
    VideoRef,
)


class YouTubePort(Protocol):
    """All methods are BLOCKING (yt-dlp/network); services call them via
    asyncio.to_thread. They raise models.YouTubeError on failure."""

    def version(self) -> str:
        """Installed yt-dlp version string."""
        ...

    def resolve_source(self, url_or_handle: str) -> SourceRef:
        """Channel URL / @handle / bare handle / playlist URL -> SourceRef."""
        ...

    def resolve_video(self, url: str) -> VideoRef:
        """watch / youtu.be / shorts URL -> VideoRef (no download)."""
        ...

    def fetch_feed(self, source: SourceRef) -> list[FeedEntry]:
        """Newest uploads, newest first (about 15). Channels and playlists both use
        the public RSS feed, so no yt-dlp call (and no bot check) is involved."""
        ...

    def download(
        self, req: DownloadRequest, on_progress: Callable[[ProgressInfo], None] | None = None
    ) -> DownloadResult:
        """Download one video into req.staging_dir (mp4 + thumbnail). Filtered videos
        return DownloadResult(video=None, skip_reason=...) instead of raising."""
        ...

    def upgrade(self) -> str:
        """pip-upgrade yt-dlp (+ the bgutil plugin); returns the new version string.
        The new version is only used after a restart (documented in the UI)."""
        ...


class LibraryWriterPort(Protocol):
    """Synchronous filesystem operations on the media library. Callers run these
    via asyncio.to_thread. Every mutating method checks the sentinel first."""

    root: Path

    def assert_online(self) -> None:
        """Raises LibraryOffline when the sentinel file is missing."""
        ...

    def install(self, rel_folder: str, file_name: str, src: Path) -> InstallResult:
        """Move/copy `src` to <root>/<rel_folder>/<file_name> atomically (write
        `.<file_name>.part`, then rename). Creates folders. Overwrites."""
        ...

    def write_bytes(self, rel_folder: str, file_name: str, data: bytes) -> None:
        """Atomically write a sidecar file (nfo, poster)."""
        ...

    def exists(self, rel_path: str) -> bool: ...

    def delete(self, rel_path: str) -> None:
        """Delete the file and sidecars sharing its stem (.nfo, -poster.jpg, ...).
        Missing files are ignored. Removes the parent folder if it ends up empty
        (never the library root)."""
        ...


class MediaServerNotifier(Protocol):
    def touch(self, rel_folder: str) -> None:
        """Note that a folder changed; the rescan request is debounced."""
        ...

    async def flush(self) -> None:
        """Send any pending rescan now (shutdown, tests)."""
        ...

    async def test(self) -> dict:
        """{"ok": bool, "detail": str}"""
        ...

    async def create_library(self) -> dict:
        """Create a Home Videos library pointing at the library path.
        {"ok": bool, "detail": str}"""
        ...
