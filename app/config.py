import os
from pathlib import Path

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "app.db"
COOKIES_FILE = DATA_DIR / "cookies.txt"
PORT = int(os.environ.get("PORT", "8000"))

# HTTP address of the bgutil-ytdlp-pot-provider sidecar (see docker-compose.yml)
# that supplies YouTube's PO tokens, needed alongside cookies for full-quality
# (non-SABR) formats to be available.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "http://bgutil-provider:4416")

DEFAULT_SETTINGS = {
    "download_subfolder": "YouTube",
    "poll_interval_minutes": "15",
    "quality": "best",
    # Downloaded videos older than this are auto-deleted (0 = never). Default ~1 month.
    "auto_delete_days": "30",
    # Auto-discovered (channel/RSS) uploads matching these are skipped, not downloaded.
    # Manually pasted links always bypass these filters.
    "skip_shorts": "true",
    "shorts_max_seconds": "180",
    "skip_live": "true",
    "max_concurrent_downloads": "2",
}

QUALITY_FORMATS = {
    "best": "bestvideo*+bestaudio/best",
    "1080p": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]",
}


def youtube_ydl_opts() -> dict:
    """Common yt-dlp options needed to authenticate as a real client: a cookies
    file (if one has been placed in DATA_DIR) plus the PO token provider that
    unlocks full-quality formats YouTube otherwise restricts to SABR streaming.
    """
    opts = {
        "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}},
    }
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts
