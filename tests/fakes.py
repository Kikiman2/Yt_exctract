"""In-memory/tmp-dir test doubles for the ports in app/ports.py. Also used by
DEV_FAKE=1 to run the UI without yt-dlp or a real library."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable

from app import config
from app.models import (
    DownloadRequest,
    DownloadResult,
    FeedEntry,
    InstallResult,
    LibraryOffline,
    ProgressInfo,
    SourceRef,
    VideoMeta,
    VideoRef,
    YouTubeError,
)


class FakeYouTube:
    """Scriptable YouTubePort.

    sources:  input string (url/handle) -> SourceRef
    videos:   input url -> VideoRef
    feeds:    source_id -> [FeedEntry] (newest first)
    fail:     video_id -> YouTubeError raised by download()
    shorts / live: video_ids that the skip filters would reject (unless req.force)
    durations: video_id -> seconds (default 600)
    """

    def __init__(self):
        self.sources: dict[str, SourceRef] = {}
        self.videos: dict[str, VideoRef] = {}
        self.feeds: dict[str, list[FeedEntry]] = {}
        self.fail: dict[str, YouTubeError] = {}
        self.shorts: set[str] = set()
        self.live: set[str] = set()
        self.durations: dict[str, int] = {}
        self.downloads: list[DownloadRequest] = []
        self.feed_errors: dict[str, YouTubeError] = {}
        self.ytdlp_version = "2099.01.01"
        self.upgraded_to: str | None = None
        self.delay = threading.Event()  # set() = download() returns immediately
        self.delay.set()
        self._lock = threading.Lock()

    # -- helpers for tests
    def add_channel(self, source_id: str, title: str, *inputs: str) -> SourceRef:
        ref = SourceRef("channel", source_id, title, f"https://www.youtube.com/channel/{source_id}")
        for i in inputs or (ref.url,):
            self.sources[i] = ref
        self.feeds.setdefault(source_id, [])
        return ref

    def add_playlist(self, source_id: str, title: str, *inputs: str) -> SourceRef:
        ref = SourceRef("playlist", source_id, title, f"https://www.youtube.com/playlist?list={source_id}")
        for i in inputs or (ref.url,):
            self.sources[i] = ref
        self.feeds.setdefault(source_id, [])
        return ref

    def add_video(self, video_id: str, title: str, channel: SourceRef | None = None) -> VideoRef:
        ref = VideoRef(video_id, title, channel.source_id if channel else None,
                       channel.title if channel else None)
        self.videos[f"https://www.youtube.com/watch?v={video_id}"] = ref
        return ref

    def publish(self, source_id: str, video_id: str, title: str) -> None:
        self.feeds.setdefault(source_id, []).insert(0, FeedEntry(video_id, title))

    # -- port
    def version(self) -> str:
        return self.ytdlp_version

    def resolve_source(self, url_or_handle: str) -> SourceRef:
        try:
            return self.sources[url_or_handle]
        except KeyError:
            raise YouTubeError(f"Could not resolve a channel or playlist from '{url_or_handle}'") from None

    def resolve_video(self, url: str) -> VideoRef:
        try:
            return self.videos[url]
        except KeyError:
            raise YouTubeError(f"Could not resolve a video from '{url}'") from None

    def fetch_feed(self, source: SourceRef) -> list[FeedEntry]:
        if source.source_id in self.feed_errors:
            raise self.feed_errors[source.source_id]
        return list(self.feeds.get(source.source_id, []))

    def download(self, req: DownloadRequest,
                 on_progress: Callable[[ProgressInfo], None] | None = None) -> DownloadResult:
        with self._lock:
            self.downloads.append(req)
        self.delay.wait(timeout=10)
        if req.video_id in self.fail:
            raise self.fail[req.video_id]
        duration = self.durations.get(req.video_id, 600)
        meta = VideoMeta(req.video_id, f"Title {req.video_id}", channel="Fake Channel",
                         channel_id="UCfake", upload_date="2024-05-06", duration=duration,
                         description="fake description", webpage_url=req.url)
        if not req.force:
            if req.skip_live and req.video_id in self.live:
                return DownloadResult(None, meta=meta, skip_reason="live stream (is live)")
            if req.skip_shorts and (req.video_id in self.shorts or duration <= req.shorts_max_seconds):
                return DownloadResult(None, meta=meta, skip_reason=f"short-form video ({duration}s)")
        req.staging_dir.mkdir(parents=True, exist_ok=True)
        video = req.staging_dir / f"{req.video_id}.mp4"
        video.write_bytes(b"fake-video-" + req.video_id.encode())
        thumb = req.staging_dir / f"{req.video_id}.jpg"
        thumb.write_bytes(b"fake-thumb")
        if on_progress:
            on_progress(ProgressInfo("downloading", 50.0, 1000.0, 5))
            on_progress(ProgressInfo("processing", 100.0))
        return DownloadResult(video, thumb, meta)

    def upgrade(self) -> str:
        self.upgraded_to = "2099.02.02"
        return self.upgraded_to


class FakeLibraryWriter:
    """Real filesystem under `root`; `online` simulates the sentinel going away."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.online = True

    def assert_online(self) -> None:
        if not self.online:
            raise LibraryOffline("sentinel file missing")

    def install(self, rel_folder: str, file_name: str, src: Path) -> InstallResult:
        self.assert_online()
        dest = self.root / rel_folder / file_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)
        return InstallResult(f"{rel_folder}/{file_name}", dest.stat().st_size)

    def write_bytes(self, rel_folder: str, file_name: str, data: bytes) -> None:
        self.assert_online()
        dest = self.root / rel_folder / file_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def exists(self, rel_path: str) -> bool:
        return (self.root / rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        self.assert_online()
        p = self.root / rel_path
        for sib in p.parent.glob(f"{p.stem}*") if p.parent.exists() else []:
            if sib.is_file():
                sib.unlink(missing_ok=True)
        parent = p.parent
        if parent != self.root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


class FakeNotifier:
    def __init__(self):
        self.touched: list[str] = []
        self.flushes = 0
        self.ok = True
        self.created = 0

    def touch(self, rel_folder: str) -> None:
        self.touched.append(rel_folder)

    async def flush(self) -> None:
        self.flushes += 1

    async def test(self) -> dict:
        return {"ok": self.ok, "detail": "fake"}

    async def create_library(self) -> dict:
        self.created += 1
        return {"ok": True, "detail": "created (fake)"}


def default_staging(tmp: Path) -> Path:
    return Path(tmp) / "staging"


__all__ = ["FakeYouTube", "FakeLibraryWriter", "FakeNotifier", "config"]
