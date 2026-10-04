"""File and folder naming for the library. Pure.

Why: names end up on SMB/NTFS shares read by Jellyfin, so Windows-invalid characters are
replaced and the whole file name stays under 200 bytes (ext4/NTFS limit is 255, and the
sidecar suffixes plus the `.part` temp name need headroom)."""

from __future__ import annotations

import re

MAX_NAME_BYTES = 200
FALLBACK_FOLDER = "Manual"

_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")


def sanitize_name(name: str) -> str:
    cleaned = _INVALID.sub("_", name)
    cleaned = _SPACES.sub(" ", cleaned).strip(" .")
    return cleaned or "Unknown"


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Cut on a character boundary so multi-byte characters are never split."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    cut = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return cut.rstrip(" .")


def video_file_name(title: str, video_id: str, ext: str = "mp4") -> str:
    suffix = f" [{video_id}].{ext}"
    budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    safe_title = _truncate_bytes(sanitize_name(title), max(budget, 1))
    return f"{safe_title or 'Unknown'}{suffix}"


def rel_folder(subfolder: str, channel_name: str | None) -> str:
    channel = sanitize_name(channel_name) if channel_name and channel_name.strip() else FALLBACK_FOLDER
    return f"{sanitize_name(subfolder)}/{channel}"


def sidecar_names(file_name: str) -> dict[str, str]:
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return {"nfo": f"{stem}.nfo", "poster": f"{stem}-poster.jpg"}
