from app.domain.naming import rel_folder, sanitize_name, sidecar_names, video_file_name


def test_ordinary_names_unchanged():
    assert sanitize_name("Veritasium") == "Veritasium"
    assert sanitize_name("Some Channel - 2024 (HD)") == "Some Channel - 2024 (HD)"


def test_windows_invalid_chars_replaced():
    assert sanitize_name('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_control_chars_and_whitespace():
    assert sanitize_name("a\x00b\tc\nd") == "a_b_c_d"
    assert sanitize_name("a   b") == "a b"


def test_strips_dots_and_spaces():
    assert sanitize_name("  ..name.. ") == "name"


def test_empty_becomes_unknown():
    assert sanitize_name("") == "Unknown"
    assert sanitize_name(" . . ") == "Unknown"


def test_unicode_kept():
    assert sanitize_name("日本語のチャンネル") == "日本語のチャンネル"
    assert sanitize_name("Café ☕") == "Café ☕"


def test_file_name_basic():
    assert video_file_name("Hello: World?", "abc123XYZ_-") == "Hello_ World_ [abc123XYZ_-].mp4"


def test_file_name_custom_ext():
    assert video_file_name("t", "id", "mkv") == "t [id].mkv"


def test_file_name_long_ascii_title_fits_200_bytes():
    name = video_file_name("x" * 500, "abc123XYZ_-")
    assert len(name.encode()) <= 200
    assert name.endswith(" [abc123XYZ_-].mp4")


def test_file_name_long_unicode_title_not_split_mid_char():
    name = video_file_name("日" * 200, "abc123XYZ_-")
    assert len(name.encode()) <= 200
    name.encode("utf-8").decode("utf-8")  # valid utf-8
    assert name.endswith(" [abc123XYZ_-].mp4")


def test_file_name_truncation_does_not_leave_trailing_space():
    name = video_file_name("a" * 170 + " " + "b" * 100, "abc123XYZ_-")
    assert " [abc" in name and "  [" not in name


def test_file_name_empty_title():
    assert video_file_name("", "vid") == "Unknown [vid].mp4"


def test_rel_folder():
    assert rel_folder("YouTube", "My: Channel") == "YouTube/My_ Channel"


def test_rel_folder_manual():
    assert rel_folder("YouTube", None) == "YouTube/Manual"
    assert rel_folder("YouTube", "  ") == "YouTube/Manual"


def test_rel_folder_sanitises_subfolder():
    assert ".." not in rel_folder("../Evil", "c").split("/")


def test_sidecar_names():
    assert sidecar_names("Title [id].mp4") == {"nfo": "Title [id].nfo", "poster": "Title [id]-poster.jpg"}


def test_sidecar_names_dotted_title():
    assert sidecar_names("v1.2 demo [id].mp4")["nfo"] == "v1.2 demo [id].nfo"
