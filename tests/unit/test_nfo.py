import xml.etree.ElementTree as ET

from app.library.nfo import poster_bytes, render_nfo
from app.models import VideoMeta


def _parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)  # raises on invalid XML


def test_full_nfo():
    meta = VideoMeta("abc123", "My Video", channel="Chan", upload_date="2024-05-06",
                     duration=605, description="line1\nline2")
    root = _parse(render_nfo(meta, "Chan"))
    assert root.tag == "movie"
    assert root.findtext("title") == "My Video"
    assert root.findtext("plot") == "line1\nline2"
    assert root.findtext("premiered") == "2024-05-06"
    assert root.findtext("year") == "2024"
    assert root.findtext("studio") == "Chan"
    assert root.findtext("runtime") == "10"
    uid = root.find("uniqueid")
    assert uid.text == "abc123" and uid.get("type") == "youtube" and uid.get("default") == "true"


def test_hostile_text_stays_valid_xml():
    title = "A & B <script> \x00\x0b\x1f\ud800 \U0001F600 ]]> \"q\" 'a'"
    desc = "bad \x08 ctrl & <b>html</b> ￾ \U0001F4A9\ttab"
    data = render_nfo(VideoMeta("id", title, description=desc), "Chan <&>")
    root = _parse(data)
    assert root.findtext("title") == "A & B <script>  \U0001F600 ]]> \"q\" 'a'"
    assert root.findtext("plot") == "bad  ctrl & <b>html</b>  \U0001F4A9\ttab"
    assert root.findtext("studio") == "Chan <&>"


def test_missing_fields_are_omitted():
    root = _parse(render_nfo(VideoMeta("id", "T", upload_date="garbage", duration=0), None))
    for tag in ("plot", "premiered", "year", "studio", "runtime"):
        assert root.find(tag) is None
    assert root.findtext("title") == "T"


def test_short_runtime_rounds_up_to_one_minute():
    assert _parse(render_nfo(VideoMeta("id", "T", duration=20), None)).findtext("runtime") == "1"


def test_channel_falls_back_to_meta():
    assert _parse(render_nfo(VideoMeta("id", "T", channel="Meta"), None)).findtext("studio") == "Meta"


def test_poster_bytes(tmp_path):
    p = tmp_path / "t.jpg"
    p.write_bytes(b"jpg")
    assert poster_bytes(p) == b"jpg"
    assert poster_bytes(tmp_path / "missing.jpg") is None
