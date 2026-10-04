import pytest

from app.domain.urls import classify_input, extract_video_id

CHANNEL = "UC" + "a" * 22


def test_bare_name_becomes_handle():
    assert classify_input("veritasium") == ("handle", "https://www.youtube.com/@veritasium")


def test_handle_with_at():
    assert classify_input("@veritasium") == ("handle", "https://www.youtube.com/@veritasium")


def test_handle_whitespace_and_dots():
    assert classify_input("  @some.name-1 ") == ("handle", "https://www.youtube.com/@some.name-1")


def test_handle_url():
    assert classify_input("https://www.youtube.com/@veritasium/") == ("channel_url", "https://www.youtube.com/@veritasium")


def test_handle_url_with_tab_kept():
    assert classify_input("https://youtube.com/@x/videos")[0] == "channel_url"


def test_channel_id_url():
    assert classify_input(f"https://www.youtube.com/channel/{CHANNEL}") == (
        "channel_url", f"https://www.youtube.com/channel/{CHANNEL}")


def test_bare_channel_id():
    assert classify_input(CHANNEL) == ("channel_url", f"https://www.youtube.com/channel/{CHANNEL}")


def test_scheme_less_youtube_url():
    assert classify_input("youtube.com/@abc") == ("channel_url", "https://www.youtube.com/@abc")


def test_playlist_url():
    assert classify_input("https://www.youtube.com/playlist?list=PLabc123&si=zzz") == (
        "playlist_url", "https://www.youtube.com/playlist?list=PLabc123")


def test_watch_url_with_list_is_playlist():
    assert classify_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc") == (
        "playlist_url", "https://www.youtube.com/playlist?list=PLabc")


def test_playlist_without_list_param():
    with pytest.raises(ValueError):
        classify_input("https://www.youtube.com/playlist")


def test_mix_playlist_rejected():
    with pytest.raises(ValueError):
        classify_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ")


@pytest.mark.parametrize("bad", ["", "   ", "two words", "@", "https://example.com/@x", "https://www.youtube.com/"])
def test_bad_inputs(bad):
    with pytest.raises(ValueError):
        classify_input(bad)


def test_video_url_rejected_as_source():
    with pytest.raises(ValueError):
        classify_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&t=10s",
    "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "www.youtube.com/watch?v=dQw4w9WgXcQ",
    "  https://youtu.be/dQw4w9WgXcQ  ",
])
def test_extract_video_id(url):
    assert extract_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "", "https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch",
    "https://www.youtube.com/watch?v=short", "https://youtu.be/", "https://www.youtube.com/@abc",
    "https://www.youtube.com/playlist?list=PLabc", "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
])
def test_extract_video_id_none(url):
    assert extract_video_id(url) is None
