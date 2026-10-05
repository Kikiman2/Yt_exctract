"""JSON shapes returned by the services layer and the HTTP API. The frontend
(templates/static) and routes/api.py are written against these; services produce
them. Changing a shape means changing it here first."""

from __future__ import annotations

from typing import TypedDict


class ProgressJson(TypedDict):
    status: str  # downloading | processing
    percent: float | None
    speed: float | None  # bytes/s
    eta: int | None  # seconds


class VideoJson(TypedDict):
    video_id: str
    url: str  # https://www.youtube.com/watch?v=<id>
    title: str
    source_id: str | None
    channel_name: str | None  # subscription title, else the snapshot, else None (manual)
    status: str  # models.VIDEO_STATUSES
    error: str | None
    attempts: int
    next_retry_at: str | None
    added_at: str
    downloaded_at: str | None
    file_path: str | None  # relative to the library root
    file_size: int | None
    upload_date: str | None
    duration: int | None
    progress: ProgressJson | None


class SubscriptionJson(TypedDict):
    id: str
    kind: str  # channel | playlist
    title: str
    url: str
    enabled: bool
    auto_delete_enabled: bool
    quality: str | None  # null = global setting
    skip_shorts: bool | None
    skip_live: bool | None
    retention_days: int | None
    last_checked_at: str | None
    last_error: str | None
    video_count: int
    downloaded_count: int


class ComponentJson(TypedDict):
    ok: bool
    detail: str


class YtdlpJson(TypedDict):
    ok: bool
    version: str | None
    detail: str


class CookiesJson(TypedDict):
    present: bool
    cookie_count: int
    youtube_cookie_count: int
    expires_at: str | None
    modified_at: str | None
    detail: str


class LibraryJson(TypedDict):
    ok: bool
    path: str
    detail: str


class DiskJson(TypedDict):
    ok: bool
    free_gb: float | None


class HealthJson(TypedDict):
    ok: bool
    ytdlp: YtdlpJson
    pot_provider: ComponentJson
    cookies: CookiesJson
    library: LibraryJson
    disk: DiskJson
    jellyfin: ComponentJson
    checked_at: str | None


class QueueJson(TypedDict):
    paused: bool
    pause_reasons: list[str]
    counts: dict[str, int]  # status -> n (all VIDEO_STATUSES present)
    active: list[VideoJson]  # status == downloading


class StatusJson(TypedDict):
    health: HealthJson
    queue: QueueJson


class EventJson(TypedDict):
    id: int
    ts: str
    level: str
    kind: str
    video_id: str | None
    message: str


class SettingsJson(TypedDict, total=False):
    """Every key of config.DEFAULT_SETTINGS as a string, plus read-only info."""

    jellyfin_url: str
    jellyfin_configured: bool
    library_path: str
