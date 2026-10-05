import os
import time

import pytest

from app.library import writer as writer_mod
from app.library.writer import LibraryWriter
from app.models import LibraryOffline

SENTINEL = ".yt-extract-library"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "library"
    r.mkdir()
    (r / SENTINEL).write_text("")
    return r


@pytest.fixture
def w(root):
    return LibraryWriter(root, SENTINEL)


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "abc.mp4"
    p.write_bytes(b"video-bytes" * 1000)
    return p


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_offline_refuses_all_mutation(tmp_path, src):
    root = tmp_path / "unmounted"
    root.mkdir()
    w = LibraryWriter(root, SENTINEL)
    for call in (lambda: w.install("Ch/Sub", "a [x].mp4", src),
                 lambda: w.write_bytes("Ch", "a.nfo", b"x"),
                 lambda: w.delete("Ch/a.mp4"),
                 lambda: w.remove_stale_parts(0)):
        with pytest.raises(LibraryOffline):
            call()
    assert list(root.iterdir()) == []
    assert src.exists()


def test_sentinel_must_be_a_file(tmp_path):
    (tmp_path / SENTINEL).mkdir()
    with pytest.raises(LibraryOffline):
        LibraryWriter(tmp_path, SENTINEL).assert_online()


def test_install_moves_file_and_reports(w, root, src):
    data = src.read_bytes()
    res = w.install("YouTube/Chan", "T [abc].mp4", src)
    assert res.rel_path == "YouTube/Chan/T [abc].mp4"
    assert res.size == len(data)
    assert (root / res.rel_path).read_bytes() == data
    assert not src.exists()
    assert w.exists(res.rel_path) and not w.exists("YouTube/Chan/nope.mp4")
    assert _files(root) == [SENTINEL, "YouTube", "YouTube/Chan", "YouTube/Chan/T [abc].mp4"]


def test_install_overwrites(w, root, src, tmp_path):
    w.install("A", "x.mp4", src)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"new")
    res = w.install("A", "x.mp4", other)
    assert res.size == 3 and (root / "A" / "x.mp4").read_bytes() == b"new"


def test_part_never_visible_as_final_name(w, src, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(a, b):
        seen.append((os.path.basename(a), os.path.exists(b)))
        return real_replace(a, b)

    monkeypatch.setattr(writer_mod.os, "replace", spy)
    w.install("A", "x.mp4", src)
    assert seen == [(".x.mp4.part", False)]


def test_failure_cleans_part_and_keeps_source(w, root, src, monkeypatch):
    def boom(a, b):
        raise OSError("share went away")

    monkeypatch.setattr(writer_mod.os, "replace", boom)
    with pytest.raises(OSError):
        w.install("A", "x.mp4", src)
    assert _files(root) == [SENTINEL, "A"]
    assert src.exists()


def test_write_bytes_is_atomic_and_clean(w, root, monkeypatch):
    w.write_bytes("A", "x.nfo", b"<movie/>")
    assert (root / "A" / "x.nfo").read_bytes() == b"<movie/>"
    monkeypatch.setattr(writer_mod.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        w.write_bytes("A", "y.nfo", b"1")
    assert _files(root) == [SENTINEL, "A", "A/x.nfo"]


@pytest.mark.parametrize("folder,name", [
    ("..", "x.mp4"), ("a/../..", "x.mp4"), ("/abs", "x.mp4"), ("ok", "../x.mp4"),
    ("ok", "a/b.mp4"), ("ok", "a\\b.mp4"), ("", "x.mp4"), ("ok", "."), ("a\\b", "x.mp4"),
    ("ok", ""), ("ok\0", "x.mp4"),
])
def test_traversal_rejected(w, root, src, folder, name):
    with pytest.raises(ValueError):
        w.install(folder, name, src)
    with pytest.raises(ValueError):
        w.write_bytes(folder, name, b"x")
    assert _files(root) == [SENTINEL]


def test_symlink_escape_rejected(w, root, src, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        w.install("link", "x.mp4", src)
    assert list(outside.iterdir()) == []


def test_delete_and_exists_traversal_safe(w, tmp_path):
    victim = tmp_path / "victim.mp4"
    victim.write_text("keep")
    with pytest.raises(ValueError):
        w.delete("../victim.mp4")
    assert w.exists("../victim.mp4") is False
    assert victim.exists()


def test_delete_removes_sidecars_and_prunes_empty_folder(w, root, src, tmp_path):
    w.install("Chan", "T [abc].mp4", src)
    for name in ("T [abc].nfo", "T [abc]-poster.jpg", "T [abc].jpg", "T [abc].webp"):
        w.write_bytes("Chan", name, b"x")
    w.write_bytes("Chan", "T [abcd].mp4", b"other video with longer id")
    w.delete("Chan/T [abc].mp4")
    assert _files(root) == [SENTINEL, "Chan", "Chan/T [abcd].mp4"]
    w.delete("Chan/T [abcd].mp4")
    assert _files(root) == [SENTINEL]  # folder pruned, root and sentinel kept


def test_delete_missing_is_noop_and_never_prunes_root(w, root):
    w.delete("Nope/x.mp4")
    w.delete("x.mp4")
    assert _files(root) == [SENTINEL]


def test_delete_keeps_folder_with_other_files(w, root):
    w.write_bytes("Chan", "a [1].mp4", b"1")
    w.write_bytes("Chan", "b [2].mp4", b"2")
    w.delete("Chan/a [1].mp4")
    assert _files(root) == [SENTINEL, "Chan", "Chan/b [2].mp4"]


def test_remove_stale_parts(w, root):
    w.write_bytes("A", "keep.mp4", b"x")
    old = root / "A" / ".old.mp4.part"
    new = root / "A" / ".new.mp4.part"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert w.remove_stale_parts(3600) == 1
    assert not old.exists() and new.exists()
    assert w.remove_stale_parts(3600) == 0
