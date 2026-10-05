"""Writes into the media library share. Implements ports.LibraryWriterPort.

The root is usually a network share that can unmount or go stale. Every mutation first
checks the sentinel file, so a missing mount never turns into files written onto the
container's own disk under the mount point."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path, PurePosixPath

from ..models import InstallResult, LibraryOffline

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def _fsync_dir(path: Path) -> None:
    # Best effort: CIFS and some filesystems refuse fsync on directories.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _parts(rel: str) -> tuple[str, ...]:
    """Split a library-relative path into safe components (no '..', no absolute, no
    backslashes). Raises ValueError so a hostile name can never leave the root."""
    if not rel or "\\" in rel or "\0" in rel:
        raise ValueError(f"unsafe path: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("", ".", "..") for part in rel.split("/")):
        raise ValueError(f"unsafe path: {rel!r}")
    return p.parts


class LibraryWriter:
    def __init__(self, root: Path, sentinel: str):
        self.root = Path(root)
        self.sentinel = sentinel

    def assert_online(self) -> None:
        try:
            ok = (self.root / self.sentinel).is_file()
        except OSError:  # stale CIFS handles raise instead of returning False
            ok = False
        if not ok:
            raise LibraryOffline(f"library sentinel missing: {self.root / self.sentinel}")

    def _resolve(self, rel: str) -> Path:
        path = self.root.joinpath(*_parts(rel))
        # Symlinks inside the library must not lead out of it either.
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(f"path escapes the library: {rel!r}") from None
        return path

    def _target(self, rel_folder: str, file_name: str) -> Path:
        if "/" in file_name or "\\" in file_name:
            raise ValueError(f"unsafe file name: {file_name!r}")
        return self._resolve(f"{rel_folder}/{file_name}")

    @staticmethod
    def _write_atomic(final: Path, write) -> None:
        """Write via a hidden .part sibling, fsync, then os.replace onto `final`, so
        readers (Jellyfin) never see a half-written file under its real name."""
        part = final.with_name(f".{final.name}.part")
        try:
            with open(part, "wb") as f:
                write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        _fsync_dir(final.parent)

    def install(self, rel_folder: str, file_name: str, src: Path) -> InstallResult:
        self.assert_online()
        final = self._target(rel_folder, file_name)
        src_size = src.stat().st_size
        final.parent.mkdir(parents=True, exist_ok=True)

        def copy(f) -> None:
            with open(src, "rb") as s:
                shutil.copyfileobj(s, f, _CHUNK)

        self._write_atomic(final, copy)
        size = final.stat().st_size
        if size != src_size:
            # A truncated copy (share filled up) must not stay under the real name.
            final.unlink(missing_ok=True)
            raise OSError(f"size mismatch after install of {final}: {size} != {src_size}")
        # Copy (not move) so this works across filesystems; the staging copy is done with.
        src.unlink(missing_ok=True)
        logger.info("Library: installed %s/%s (%d bytes)", rel_folder, file_name, size)
        return InstallResult(rel_path=f"{rel_folder}/{file_name}", size=size)

    def write_bytes(self, rel_folder: str, file_name: str, data: bytes) -> None:
        self.assert_online()
        final = self._target(rel_folder, file_name)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(final, lambda f: f.write(data))

    def exists(self, rel_path: str) -> bool:
        try:
            return self._resolve(rel_path).is_file()
        except (ValueError, OSError):
            return False

    def delete(self, rel_path: str) -> None:
        self.assert_online()
        path = self._resolve(rel_path)
        parent = path.parent
        if parent.is_dir():
            stem = path.stem
            for sib in parent.iterdir():
                # "<stem>.ext" and "<stem>-poster.ext" only: a plain prefix match could
                # take another video whose title merely starts the same.
                rest = sib.name[len(stem):]
                if sib.name.startswith(stem) and rest[:1] in (".", "-") and sib.is_file():
                    sib.unlink(missing_ok=True)
            if parent != self.root and not any(parent.iterdir()):
                parent.rmdir()
        logger.info("Library: deleted %s", rel_path)

    def remove_stale_parts(self, max_age_seconds: float) -> int:
        """Remove `.<name>.part` leftovers from crashed installs; returns how many."""
        self.assert_online()
        cutoff = time.time() - max_age_seconds
        removed = 0
        for part in self.root.rglob(".*.part"):
            try:
                if part.is_file() and part.stat().st_mtime <= cutoff:
                    part.unlink()
                    removed += 1
            except OSError:
                logger.warning("Library: could not remove stale %s", part)
        return removed
