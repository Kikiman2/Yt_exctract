"""Jellyfin sidecars: a `<movie>` NFO and the poster image. Pure functions, no library I/O."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import VideoMeta

# XML 1.0 forbids most control chars (and lone surrogates); a YouTube title or description
# can contain them, and one stray \x0b would make the whole NFO unparseable.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _clean(text: str) -> str:
    return _INVALID_XML.sub("", text)


def render_nfo(meta: VideoMeta, channel: str | None) -> bytes:
    root = ET.Element("movie")

    def add(tag: str, text: str | None, **attrs: str) -> None:
        if text:
            ET.SubElement(root, tag, attrs).text = _clean(text)

    add("title", meta.title)
    add("plot", meta.description)
    if meta.upload_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.upload_date):
        add("premiered", meta.upload_date)
        add("year", meta.upload_date[:4])
    add("studio", channel or meta.channel)
    if meta.duration and meta.duration > 0:
        add("runtime", str(max(1, round(meta.duration / 60))))
    add("uniqueid", meta.video_id, type="youtube", default="true")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def poster_bytes(src: Path) -> bytes | None:
    try:
        return Path(src).read_bytes()
    except OSError:
        return None
