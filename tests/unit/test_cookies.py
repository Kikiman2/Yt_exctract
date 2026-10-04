import json
import os

import pytest

from app.models import CookiesInvalid
from app.youtube.cookies import FAR_FUTURE_EXPIRY, inspect, parse_cookies

LINE = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc"


def netscape(*lines):
    return ("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n").encode()


def rows(text):
    return [l.split("\t") for l in text.splitlines() if l and not l.startswith("#")]


def test_netscape_passthrough():
    out = parse_cookies(netscape(LINE))
    assert rows(out) == [LINE.split("\t")]
    assert out.startswith("# Netscape HTTP Cookie File")


def test_netscape_crlf_and_bom():
    out = parse_cookies(b"\xef\xbb\xbf" + netscape(LINE).replace(b"\n", b"\r\n"))
    assert "\r" not in out and len(rows(out)) == 1


def test_netscape_httponly_lines_kept():
    out = parse_cookies(netscape("#HttpOnly_" + LINE))
    assert "#HttpOnly_.youtube.com" in out


def test_netscape_without_header_still_ok():
    assert len(rows(parse_cookies(LINE.encode()))) == 1


def test_json_list_converted():
    data = [{"domain": ".youtube.com", "name": "SID", "value": "v", "path": "/", "secure": True,
             "expirationDate": 1893456000.5}]
    out = parse_cookies(json.dumps(data).encode())
    assert rows(out) == [[".youtube.com", "TRUE", "/", "TRUE", "1893456000", "SID", "v"]]


def test_json_wrapped_in_cookies_key_playwright():
    data = {"cookies": [{"domain": ".youtube.com", "name": "a", "value": "b", "expires": -1}], "origins": []}
    out = parse_cookies(json.dumps(data).encode())
    assert rows(out)[0][4] == str(FAR_FUTURE_EXPIRY)


def test_json_host_only_flag():
    data = [{"domain": "www.youtube.com", "name": "a", "value": "b", "hostOnly": True},
            {"domain": "youtube.com", "name": "c", "value": "d", "hostOnly": False}]
    r = rows(parse_cookies(json.dumps(data).encode()))
    assert r[0][:2] == ["www.youtube.com", "FALSE"]
    assert r[1][:2] == [".youtube.com", "TRUE"]


def test_json_filters_to_youtube_when_present():
    data = [{"domain": ".youtube.com", "name": "a", "value": "1"}, {"domain": ".example.com", "name": "b", "value": "2"}]
    assert len(rows(parse_cookies(json.dumps(data).encode()))) == 1


def test_json_without_youtube_keeps_all():
    data = [{"domain": ".example.com", "name": "b", "value": "2"}]
    assert len(rows(parse_cookies(json.dumps(data).encode()))) == 1


def test_json_skips_incomplete_entries_and_bad_expiry():
    data = [{"domain": ".youtube.com", "name": "a"}, "junk", {"domain": ".youtube.com", "name": "b", "value": "", "expiry": "x"}]
    r = rows(parse_cookies(json.dumps(data).encode()))
    assert len(r) == 1 and r[0][4] == str(FAR_FUTURE_EXPIRY)


@pytest.mark.parametrize("bad", [
    b"", b"   \n", b"hello world", b"\xff\xfe\x00bad", b"{not json", b"[]", b'{"cookies": []}',
    b'{"foo": 1}', b'"string"', b"[{}]", b"# Netscape HTTP Cookie File\n# only comments\n",
    b"a\tb\tc\n",
])
def test_invalid_inputs(bad):
    with pytest.raises(CookiesInvalid):
        parse_cookies(bad)


def test_inspect_missing_file(tmp_path):
    status = inspect(tmp_path / "nope.txt")
    assert status.present is False and status.cookie_count == 0


def test_inspect_counts_and_earliest_expiry(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(netscape(
        ".youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tx",
        ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tLOGIN_INFO\tx",
        ".youtube.com\tTRUE\t/\tTRUE\t1700000000\tPREF\tx",
        ".google.com\tTRUE\t/\tTRUE\t1850000000\tSID\tx",
        ".example.com\tTRUE\t/\tFALSE\t0\tfoo\tx",
    ))
    os.utime(p, (1_700_000_000, 1_700_000_000))
    s = inspect(p)
    assert (s.present, s.cookie_count, s.youtube_cookie_count) == (True, 5, 3)
    assert s.expires_at == "2027-01-15"  # 1800000000
    assert s.modified_at == "2023-11-14T22:13:20"
    assert "3 login" in s.detail


def test_inspect_no_login_cookies(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(netscape(".youtube.com\tTRUE\t/\tTRUE\t1893456000\tPREF\tx"))
    s = inspect(p)
    assert s.expires_at is None and "not signed in" in s.detail


def test_inspect_garbage_file(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(b"\xff\xfe\x00\x01")
    s = inspect(p)
    assert s.present and s.cookie_count == 0


def test_inspect_handles_httponly_prefix(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(netscape("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tx"))
    assert inspect(p).cookie_count == 1
