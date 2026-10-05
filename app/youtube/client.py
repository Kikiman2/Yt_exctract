"""yt-dlp / YouTube RSS adapter implementing ports.YouTubePort.

Why the seams: every network or subprocess touch lives in `_extract`, `_fetch_feed_text`,
`_pip_install` and `_installed_version`, so tests monkeypatch those four and never need
the network or a real yt-dlp run. All methods block; services call them via to_thread."""

from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable

import feedparser
import httpx
import yt_dlp
from yt_dlp.utils import YoutubeDLError

from .. import config
from ..domain.filters import skip_reason
from ..domain.urls import classify_input, extract_video_id
from ..models import (
    DownloadRequest,
    DownloadResult,
    FeedEntry,
    ProgressInfo,
    SourceRef,
    VideoMeta,
    VideoRef,
    YouTubeError,
)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?{kind}_id={source_id}"
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_BOT_CHECK = re.compile(r"confirm you.{1,8}re not a bot", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VIDEO_EXTS = ("mp4", "mkv", "webm", "mov")
_THUMB_EXTS = ("jpg", "jpeg", "png", "webp")
_PIP_PACKAGES = ["yt-dlp[default]", "bgutil-ytdlp-pot-provider"]


# -- seams (monkeypatched in tests)

def _extract(opts: dict, url: str, *, download: bool = False, process: bool = True) -> dict | None:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download, process=process)


def _fetch_feed_text(url: str) -> str:
    response = httpx.get(url, timeout=20, follow_redirects=True)
    if response.status_code != 200:
        raise YouTubeError(f"Feed request failed: HTTP {response.status_code}")
    return response.text


def _pip_install(packages: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", *packages],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise YouTubeError(f"pip upgrade failed: {(proc.stderr or proc.stdout).strip()[-300:]}")


def _installed_version() -> str:
    # importlib.metadata sees a fresh pip install; yt_dlp.version would be stale until restart.
    return metadata.version("yt-dlp")


# -- helpers

def _clean(message: str) -> str:
    message = _ANSI.sub("", message).strip()
    return message[len("ERROR: "):] if message.startswith("ERROR: ") else message


def _to_error(exc: Exception) -> YouTubeError:
    message = _clean(str(exc)) or exc.__class__.__name__
    return YouTubeError(message, bot_check=bool(_BOT_CHECK.search(message)))


def base_opts() -> dict:
    """Options common to every call: PO-token provider plus cookies when uploaded."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [config.POT_PROVIDER_URL]}},
    }
    if config.COOKIES_FILE.is_file():
        opts["cookiefile"] = str(config.COOKIES_FILE)
    return opts


def _run_extract(opts: dict, url: str, **kwargs) -> dict | None:
    try:
        return _extract(opts, url, **kwargs)
    except YouTubeError:
        raise
    except (YoutubeDLError, OSError) as exc:
        raise _to_error(exc) from exc


def _meta_from_info(info: dict, fallback_id: str) -> VideoMeta:
    upload = info.get("upload_date")
    if isinstance(upload, str) and re.fullmatch(r"\d{8}", upload):
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    else:
        upload = None
    duration = info.get("duration")
    return VideoMeta(
        video_id=info.get("id") or fallback_id,
        title=info.get("title") or fallback_id,
        channel=info.get("channel") or info.get("uploader"),
        channel_id=info.get("channel_id"),
        upload_date=upload,
        duration=int(duration) if duration is not None else None,
        description=info.get("description"),
        webpage_url=info.get("webpage_url"),
    )


def _find_outputs(staging_dir: Path, video_id: str) -> tuple[Path | None, Path | None]:
    video = next((p for ext in _VIDEO_EXTS if (p := staging_dir / f"{video_id}.{ext}").is_file()), None)
    thumb = next((p for ext in _THUMB_EXTS if (p := staging_dir / f"{video_id}.{ext}").is_file()), None)
    return video, thumb


class YtDlpClient:
    def version(self) -> str:
        return _installed_version()

    def resolve_source(self, url_or_handle: str) -> SourceRef:
        try:
            kind, url = classify_input(url_or_handle)
        except ValueError as exc:
            raise YouTubeError(str(exc)) from exc
        opts = {**base_opts(), "extract_flat": True, "playlistend": 1}
        info = _run_extract(opts, url) or {}
        if kind == "playlist_url":
            playlist_id = info.get("id")
            if not playlist_id:
                raise YouTubeError(f"Could not resolve a playlist from '{url_or_handle}'")
            title = info.get("title") or playlist_id
            return SourceRef("playlist", playlist_id, title, f"https://www.youtube.com/playlist?list={playlist_id}")
        channel_id = info.get("channel_id") or info.get("id")
        if not channel_id or not _CHANNEL_ID.match(channel_id):
            raise YouTubeError(f"Could not resolve a channel id from '{url_or_handle}'")
        title = info.get("channel") or info.get("title") or info.get("uploader") or channel_id
        return SourceRef("channel", channel_id, title, f"https://www.youtube.com/channel/{channel_id}")

    def resolve_video(self, url: str) -> VideoRef:
        if extract_video_id(url) is None:
            raise YouTubeError(f"Not a YouTube video URL: '{url}'")
        opts = {**base_opts(), "skip_download": True, "noplaylist": True}
        info = _run_extract(opts, url, process=False) or {}
        video_id = info.get("id")
        if not video_id:
            raise YouTubeError(f"Could not resolve a video id from '{url}'")
        return VideoRef(
            video_id,
            info.get("title") or video_id,
            info.get("channel_id"),
            info.get("channel") or info.get("uploader"),
        )

    def fetch_feed(self, source: SourceRef) -> list[FeedEntry]:
        url = FEED_URL.format(kind=source.kind, source_id=source.source_id)
        try:
            text = _fetch_feed_text(url)
        except YouTubeError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise YouTubeError(f"Feed request failed: {exc}") from exc
        feed = feedparser.parse(text)
        entries = [
            FeedEntry(e["yt_videoid"], e["title"], e.get("published"))
            for e in feed.entries
            if e.get("yt_videoid") and e.get("title")
        ]
        if feed.bozo and not feed.entries:
            raise YouTubeError(f"Feed for {source.source_id} could not be parsed")
        return entries

    def download(
        self, req: DownloadRequest, on_progress: Callable[[ProgressInfo], None] | None = None
    ) -> DownloadResult:
        captured: dict = {}

        def progress_hook(d: dict) -> None:
            if on_progress is None:
                return
            status = d.get("status")
            if status == "downloading":
                done = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                on_progress(ProgressInfo(
                    "downloading",
                    round(done / total * 100, 1) if total else None,
                    d.get("speed"),
                    int(d["eta"]) if d.get("eta") is not None else None,
                ))
            elif status == "finished":
                on_progress(ProgressInfo("processing", 100.0))

        def match_filter(info: dict, *, incomplete: bool = False) -> str | None:
            if not incomplete:
                captured["info"] = info
            reason = skip_reason(
                info,
                skip_shorts=req.skip_shorts,
                shorts_max_seconds=req.shorts_max_seconds,
                skip_live=req.skip_live,
                force=req.force,
            )
            if reason:
                captured["skip_reason"] = reason
            return reason

        req.staging_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            **base_opts(),
            "noplaylist": True,
            "format": config.QUALITY_FORMATS.get(req.quality, "best"),
            "outtmpl": str(req.staging_dir / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
            "progress_hooks": [progress_hook],
            "match_filter": match_filter,
        }
        info = _run_extract(opts, req.url, download=True)

        source_info = info or captured.get("info")
        meta = _meta_from_info(source_info, req.video_id) if source_info else None
        if captured.get("skip_reason"):
            return DownloadResult(video=None, meta=meta, skip_reason=captured["skip_reason"])

        video, thumb = _find_outputs(req.staging_dir, req.video_id)
        if video is None:
            raise YouTubeError(f"yt-dlp finished but no video file was written for {req.video_id}")
        return DownloadResult(video=video, thumbnail=thumb, meta=meta)

    def upgrade(self) -> str:
        try:
            _pip_install(_PIP_PACKAGES)
        except (subprocess.SubprocessError, OSError) as exc:
            raise YouTubeError(f"pip upgrade failed: {exc}") from exc
        return _installed_version()
