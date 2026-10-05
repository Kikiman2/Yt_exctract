import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "app.db"
STAGING_DIR = Path(os.environ.get("STAGING_DIR", str(DATA_DIR / "staging")))
COOKIES_FILE = DATA_DIR / "cookies.txt"
PORT = int(os.environ.get("PORT", "8000"))

# Library root as seen inside this container (host: MEDIA_ROOT from .env).
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media"))
# Written once by scripts/setup.sh. Its absence means the share isn't mounted (or the
# bind mount went stale) and nothing may be written to MEDIA_ROOT.
LIBRARY_SENTINEL = ".yt-extract-library"

# bgutil-ytdlp-pot-provider sidecar: PO tokens for full-quality (non-SABR) formats.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://bgutil-provider:4416")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
# Reverse proxies (IPs or CIDRs, comma-separated) whose X-Forwarded-For is believed.
TRUSTED_PROXIES = [p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()]

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://host.docker.internal:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
# The library folder as Jellyfin's own container sees it.
JELLYFIN_LIBRARY_PATH = os.environ.get("JELLYFIN_LIBRARY_PATH", "/data/YouTube")
JELLYFIN_LIBRARY_NAME = os.environ.get("JELLYFIN_LIBRARY_NAME", "YouTube")

# Development mode: wire in-memory fakes instead of real yt-dlp / library.
DEV_FAKE = os.environ.get("DEV_FAKE", "") == "1"

# Subscription source kinds.
SOURCE_KINDS = ("channel", "playlist")

DEFAULT_SETTINGS = {
    # Folder name *inside* MEDIA_ROOT that videos are saved under (compat: unchanged).
    "download_subfolder": "YouTube",
    "poll_interval_minutes": "15",
    "quality": "best",
    # Downloaded videos older than this are auto-deleted (0 = never).
    "auto_delete_days": "30",
    # Auto-discovered uploads matching these are skipped. Manual links bypass them.
    "skip_shorts": "true",
    "shorts_max_seconds": "180",
    "skip_live": "true",
    "max_concurrent_downloads": "2",
    # Failed downloads are retried automatically after this many hours, up to
    # max_attempts total attempts (then they stay failed until retried by hand).
    "retry_failed_after_hours": "6",
    "max_attempts": "5",
    "min_free_gb": "5",
    "write_nfo": "true",
    "pipeline_paused": "false",
}

QUALITY_FORMATS = {
    "best": "bestvideo*+bestaudio/best",
    "1080p": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo*[height<=720]+bestaudio/best[height<=720]",
}
