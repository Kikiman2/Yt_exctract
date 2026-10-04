"""Holds the concrete adapters the services use. runtime.startup() fills it (real
yt-dlp client / library writer / Jellyfin notifier, or tests.fakes when DEV_FAKE=1);
tests fill it with tests/fakes.py."""

from __future__ import annotations

from dataclasses import dataclass

from ..ports import LibraryWriterPort, MediaServerNotifier, YouTubePort


@dataclass
class Context:
    youtube: YouTubePort
    writer: LibraryWriterPort
    notifier: MediaServerNotifier


_ctx: Context | None = None


def set_context(ctx: Context) -> None:
    global _ctx
    _ctx = ctx


def ctx() -> Context:
    if _ctx is None:
        raise RuntimeError("services context not initialised")
    return _ctx


def has_context() -> bool:
    return _ctx is not None
