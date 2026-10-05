"""Decide whether a discovered video should be skipped (shorts, live streams). Pure.

Why: runs inside yt-dlp's match_filter, where the info dict is all we have. Manual links
(`force`) bypass every filter."""

from __future__ import annotations


def skip_reason(
    info: dict,
    *,
    skip_shorts: bool,
    shorts_max_seconds: int,
    skip_live: bool,
    force: bool,
) -> str | None:
    if force:
        return None
    live_status = info.get("live_status")
    if skip_live and live_status in ("is_live", "is_upcoming"):
        return f"live stream ({live_status.replace('_', ' ')})"
    if skip_shorts:
        duration = info.get("duration")
        # yt-dlp's media_type (from YouTube's isShortsEligible flag) is authoritative;
        # URL/duration is only a fallback for versions that don't expose it.
        media_type = info.get("media_type")
        if media_type is not None:
            is_short = media_type == "short"
        else:
            is_short = "/shorts/" in (info.get("webpage_url") or "") or (
                duration is not None and duration <= shorts_max_seconds
            )
        if is_short:
            return f"short-form video ({duration}s)" if duration is not None else "short-form video"
    return None
