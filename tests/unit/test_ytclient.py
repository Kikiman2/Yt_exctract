"""YtDlpClient with every network/subprocess seam monkeypatched."""

import pytest
import yt_dlp.utils

from app import config
from app.models import DownloadRequest, ProgressInfo, SourceRef, YouTubeError
from app.youtube import client as yc

CHANNEL = "UC" + "a" * 22
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry><yt:videoId>vid00000001</yt:videoId><title>First</title><published>2026-10-01T10:00:00+00:00</published></entry>
 <entry><yt:videoId>vid00000002</yt:videoId><title>Second</title></entry>
 <entry><title>No id</title></entry>
</feed>"""


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Records _extract calls; tests set `calls.result` or `calls.side_effect`."""
    monkeypatch.setattr(config, "COOKIES_FILE", tmp_path / "cookies.txt")

    class Recorder:
        result: dict | None = {}
        side_effect = None
        log: list = []

    rec = Recorder()
    rec.log = []

    def fake_extract(opts, url, **kwargs):
        rec.log.append((opts, url, kwargs))
        if rec.side_effect:
            return rec.side_effect(opts, url, **kwargs)
        return rec.result

    monkeypatch.setattr(yc, "_extract", fake_extract)
    return rec


def make_req(tmp_path, **kw):
    base = dict(video_id="vid00000001", url="https://www.youtube.com/watch?v=vid00000001",
                staging_dir=tmp_path / "stage", quality="best", skip_shorts=True,
                shorts_max_seconds=180, skip_live=True, force=False)
    return DownloadRequest(**{**base, **kw})


# -- options

def test_base_opts_include_pot_provider_and_cookies_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COOKIES_FILE", tmp_path / "c.txt")
    opts = yc.base_opts()
    assert opts["extractor_args"] == {"youtubepot-bgutilhttp": {"base_url": [config.POT_PROVIDER_URL]}}
    assert "cookiefile" not in opts
    (tmp_path / "c.txt").write_text("x")
    assert yc.base_opts()["cookiefile"] == str(tmp_path / "c.txt")


# -- resolve

def test_resolve_channel(calls):
    calls.result = {"channel_id": CHANNEL, "channel": "Veritasium", "title": "Veritasium - Videos"}
    ref = yc.YtDlpClient().resolve_source("veritasium")
    assert ref == SourceRef("channel", CHANNEL, "Veritasium", f"https://www.youtube.com/channel/{CHANNEL}")
    opts, url, _ = calls.log[0]
    assert url == "https://www.youtube.com/@veritasium" and opts["extract_flat"]


def test_resolve_channel_falls_back_to_id_field(calls):
    calls.result = {"id": CHANNEL, "title": "T"}
    assert yc.YtDlpClient().resolve_source("@t").source_id == CHANNEL


def test_resolve_channel_without_valid_id(calls):
    calls.result = {"id": "nope", "title": "T"}
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().resolve_source("@t")


def test_resolve_playlist(calls):
    calls.result = {"id": "PLabc", "title": "My list"}
    ref = yc.YtDlpClient().resolve_source("https://www.youtube.com/playlist?list=PLabc")
    assert ref == SourceRef("playlist", "PLabc", "My list", "https://www.youtube.com/playlist?list=PLabc")


def test_resolve_source_bad_input_is_youtube_error(calls):
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().resolve_source("https://example.com/x")
    assert not calls.log


def test_resolve_video(calls):
    calls.result = {"id": "dQw4w9WgXcQ", "title": "Song", "channel_id": CHANNEL, "channel": "Rick"}
    ref = yc.YtDlpClient().resolve_video("https://youtu.be/dQw4w9WgXcQ")
    assert (ref.video_id, ref.title, ref.channel_id, ref.channel_title) == ("dQw4w9WgXcQ", "Song", CHANNEL, "Rick")
    assert calls.log[0][2]["process"] is False


def test_resolve_video_rejects_non_video_url(calls):
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().resolve_video("https://www.youtube.com/@abc")


def test_resolve_video_no_id(calls):
    calls.result = {}
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().resolve_video("https://youtu.be/dQw4w9WgXcQ")


# -- errors

def test_download_error_mapped_with_bot_check(calls):
    def boom(*a, **k):
        raise yt_dlp.utils.DownloadError("ERROR: [youtube] x: Sign in to confirm you’re not a bot")
    calls.side_effect = boom
    with pytest.raises(YouTubeError) as exc:
        yc.YtDlpClient().resolve_video("https://youtu.be/dQw4w9WgXcQ")
    assert exc.value.bot_check is True
    assert not str(exc.value).startswith("ERROR")


def test_plain_download_error_not_bot_check(calls):
    def boom(*a, **k):
        raise yt_dlp.utils.DownloadError("Video unavailable")
    calls.side_effect = boom
    with pytest.raises(YouTubeError) as exc:
        yc.YtDlpClient().resolve_video("https://youtu.be/dQw4w9WgXcQ")
    assert exc.value.bot_check is False and str(exc.value) == "Video unavailable"


def test_ascii_apostrophe_bot_check():
    assert yc._to_error(Exception("confirm you're not a bot")).bot_check


# -- feeds

def test_fetch_feed_channel(monkeypatch):
    seen = []
    monkeypatch.setattr(yc, "_fetch_feed_text", lambda url: seen.append(url) or FEED)
    ref = SourceRef("channel", CHANNEL, "T", "u")
    entries = yc.YtDlpClient().fetch_feed(ref)
    assert [(e.video_id, e.title) for e in entries] == [("vid00000001", "First"), ("vid00000002", "Second")]
    assert entries[0].published == "2026-10-01T10:00:00+00:00"
    assert seen == [f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"]


def test_fetch_feed_playlist_url(monkeypatch):
    seen = []
    monkeypatch.setattr(yc, "_fetch_feed_text", lambda url: seen.append(url) or FEED)
    yc.YtDlpClient().fetch_feed(SourceRef("playlist", "PLabc", "T", "u"))
    assert seen == ["https://www.youtube.com/feeds/videos.xml?playlist_id=PLabc"]


def test_fetch_feed_empty_but_valid(monkeypatch):
    monkeypatch.setattr(yc, "_fetch_feed_text", lambda url: '<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    assert yc.YtDlpClient().fetch_feed(SourceRef("channel", CHANNEL, "T", "u")) == []


def test_fetch_feed_garbage_raises(monkeypatch):
    monkeypatch.setattr(yc, "_fetch_feed_text", lambda url: "<html><body>not a feed")
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().fetch_feed(SourceRef("channel", CHANNEL, "T", "u"))


def test_fetch_feed_http_error_propagates(monkeypatch):
    def fail(url):
        raise YouTubeError("Feed request failed: HTTP 404")
    monkeypatch.setattr(yc, "_fetch_feed_text", fail)
    with pytest.raises(YouTubeError, match="404"):
        yc.YtDlpClient().fetch_feed(SourceRef("channel", CHANNEL, "T", "u"))


def test_fetch_feed_network_exception_wrapped(monkeypatch):
    import httpx

    def fail(url):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(yc, "_fetch_feed_text", fail)
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().fetch_feed(SourceRef("channel", CHANNEL, "T", "u"))


def test_fetch_feed_text_seam_non_200(monkeypatch):
    class R:
        status_code = 500
        text = ""
    monkeypatch.setattr(yc.httpx, "get", lambda *a, **k: R())
    with pytest.raises(YouTubeError, match="500"):
        yc._fetch_feed_text("https://x")


# -- download

def write_outputs(req, video=True, thumb="jpg"):
    req.staging_dir.mkdir(parents=True, exist_ok=True)
    if video:
        (req.staging_dir / f"{req.video_id}.mp4").write_bytes(b"v")
    if thumb:
        (req.staging_dir / f"{req.video_id}.{thumb}").write_bytes(b"t")


INFO = {"id": "vid00000001", "title": "Hello", "channel": "Chan", "channel_id": CHANNEL,
        "upload_date": "20260930", "duration": 600.0, "description": "d",
        "webpage_url": "https://www.youtube.com/watch?v=vid00000001"}


def test_download_builds_opts_and_collects_result(calls, tmp_path):
    req = make_req(tmp_path, quality="720p")

    def fake(opts, url, **kw):
        write_outputs(req)
        return INFO
    calls.side_effect = fake
    res = yc.YtDlpClient().download(req)

    opts, url, kw = calls.log[0]
    assert url == req.url and kw["download"] is True
    assert opts["format"] == config.QUALITY_FORMATS["720p"]
    assert opts["outtmpl"] == str(req.staging_dir / "%(id)s.%(ext)s")
    assert opts["merge_output_format"] == "mp4" and opts["writethumbnail"] is True
    assert opts["noplaylist"] is True
    assert res.video == req.staging_dir / "vid00000001.mp4"
    assert res.thumbnail == req.staging_dir / "vid00000001.jpg"
    assert res.skip_reason is None
    assert (res.meta.title, res.meta.channel, res.meta.upload_date, res.meta.duration) == ("Hello", "Chan", "2026-09-30", 600)


def test_download_unknown_quality_falls_back_to_best(calls, tmp_path):
    req = make_req(tmp_path, quality="8k")
    calls.side_effect = lambda *a, **k: (write_outputs(req), INFO)[1]
    yc.YtDlpClient().download(req)
    assert calls.log[0][0]["format"] == "best"


def test_download_webp_thumbnail_and_no_thumbnail(calls, tmp_path):
    req = make_req(tmp_path)
    calls.side_effect = lambda *a, **k: (write_outputs(req, thumb="webp"), INFO)[1]
    assert yc.YtDlpClient().download(req).thumbnail.suffix == ".webp"
    for p in req.staging_dir.glob("*"):
        p.unlink()
    calls.side_effect = lambda *a, **k: (write_outputs(req, thumb=None), INFO)[1]
    assert yc.YtDlpClient().download(req).thumbnail is None


def test_download_missing_video_file_raises(calls, tmp_path):
    req = make_req(tmp_path)
    calls.side_effect = lambda *a, **k: (write_outputs(req, video=False), INFO)[1]
    with pytest.raises(YouTubeError, match="no video file"):
        yc.YtDlpClient().download(req)


def test_download_progress_hooks(calls, tmp_path):
    req = make_req(tmp_path)
    seen: list[ProgressInfo] = []

    def fake(opts, url, **kw):
        hook = opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 10.0, "eta": 15})
        hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes_estimate": 10})
        hook({"status": "downloading", "downloaded_bytes": 5})
        hook({"status": "finished"})
        hook({"status": "error"})
        write_outputs(req)
        return INFO
    calls.side_effect = fake
    yc.YtDlpClient().download(req, seen.append)
    assert seen == [ProgressInfo("downloading", 25.0, 10.0, 15), ProgressInfo("downloading", 50.0, None, None),
                    ProgressInfo("downloading", None, None, None), ProgressInfo("processing", 100.0)]


def test_download_progress_hook_without_callback_is_noop(calls, tmp_path):
    req = make_req(tmp_path)

    def fake(opts, url, **kw):
        opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
        write_outputs(req)
        return INFO
    calls.side_effect = fake
    yc.YtDlpClient().download(req)


def run_filter(calls, tmp_path, info, **req_kw):
    req = make_req(tmp_path, **req_kw)

    def fake(opts, url, **kw):
        reason = opts["match_filter"](info)
        if reason is None:
            write_outputs(req)
            return info
        return None  # yt-dlp returns nothing for a filtered entry
    calls.side_effect = fake
    return yc.YtDlpClient().download(req)


def test_download_skips_short_via_match_filter(calls, tmp_path):
    res = run_filter(calls, tmp_path, {**INFO, "duration": 30, "media_type": "short"})
    assert res.video is None and res.skip_reason == "short-form video (30s)"
    assert res.meta.title == "Hello"


def test_download_skips_live(calls, tmp_path):
    res = run_filter(calls, tmp_path, {**INFO, "live_status": "is_upcoming"})
    assert res.skip_reason == "live stream (is upcoming)" and res.video is None


def test_download_force_bypasses_filters(calls, tmp_path):
    res = run_filter(calls, tmp_path, {**INFO, "live_status": "is_live", "duration": 5}, force=True)
    assert res.skip_reason is None and res.video is not None


def test_download_filters_respect_request_settings(calls, tmp_path):
    res = run_filter(calls, tmp_path, {**INFO, "duration": 100}, skip_shorts=False, skip_live=False)
    assert res.video is not None
    res = run_filter(calls, tmp_path, {**INFO, "duration": 100}, shorts_max_seconds=60)
    assert res.video is not None


def test_match_filter_incomplete_call_does_not_capture(calls, tmp_path):
    req = make_req(tmp_path)

    def fake(opts, url, **kw):
        assert opts["match_filter"]({"id": "x"}, incomplete=True) is None
        write_outputs(req)
        return INFO
    calls.side_effect = fake
    assert yc.YtDlpClient().download(req).meta.title == "Hello"


def test_download_error_mapped(calls, tmp_path):
    def boom(*a, **k):
        raise yt_dlp.utils.DownloadError("ERROR: unable to download")
    calls.side_effect = boom
    with pytest.raises(YouTubeError, match="unable to download"):
        yc.YtDlpClient().download(make_req(tmp_path))


def test_download_creates_staging_dir(calls, tmp_path):
    req = make_req(tmp_path)

    def fake(opts, url, **kw):
        assert req.staging_dir.is_dir()
        write_outputs(req)
        return INFO
    calls.side_effect = fake
    yc.YtDlpClient().download(req)


def test_meta_handles_missing_fields():
    meta = yc._meta_from_info({"upload_date": "garbage"}, "vid")
    assert (meta.video_id, meta.title, meta.upload_date, meta.duration) == ("vid", "vid", None, None)


# -- version / upgrade

def test_version(monkeypatch):
    monkeypatch.setattr(yc, "_installed_version", lambda: "2026.1.1")
    assert yc.YtDlpClient().version() == "2026.1.1"


def test_upgrade_installs_both_packages(monkeypatch):
    installed = []
    monkeypatch.setattr(yc, "_pip_install", installed.append)
    monkeypatch.setattr(yc, "_installed_version", lambda: "2026.2.2")
    assert yc.YtDlpClient().upgrade() == "2026.2.2"
    assert installed == [["yt-dlp[default]", "bgutil-ytdlp-pot-provider"]]


def test_upgrade_failure(monkeypatch):
    def fail(pkgs):
        raise YouTubeError("pip upgrade failed: no network")
    monkeypatch.setattr(yc, "_pip_install", fail)
    with pytest.raises(YouTubeError, match="pip"):
        yc.YtDlpClient().upgrade()


def test_pip_install_seam(monkeypatch):
    import subprocess

    class P:
        returncode = 1
        stderr = "boom"
        stdout = ""
    seen = {}
    monkeypatch.setattr(yc.subprocess, "run", lambda cmd, **k: seen.setdefault("cmd", cmd) and P())
    with pytest.raises(YouTubeError, match="boom"):
        yc._pip_install(["yt-dlp[default]"])
    assert seen["cmd"][1:5] == ["-m", "pip", "install", "--upgrade"]

    def timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(yc.subprocess, "run", timeout)
    with pytest.raises(YouTubeError):
        yc.YtDlpClient().upgrade()
