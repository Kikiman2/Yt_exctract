"""Jellyfin client and debounced rescan notifier. Implements ports.MediaServerNotifier.

Failures never propagate out of touch()/flush(): a missed rescan is not worth failing a
download, so errors are logged and the folders stay pending for the next attempt."""

from __future__ import annotations

import asyncio
import logging
import threading

import httpx

logger = logging.getLogger(__name__)

NO_KEY = "JELLYFIN_API_KEY not set"


class JellyfinNotifier:
    def __init__(self, url: str, api_key: str, library_path: str, library_name: str = "YouTube",
                 debounce: float = 10, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.library_path = library_path.rstrip("/") or "/"
        self.library_name = library_name
        self.debounce = debounce
        self._pending: set[str] = set()
        self._lock = threading.Lock()  # touch() is called from worker threads
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(
            base_url=self.url, timeout=15, transport=transport,
            headers={"Authorization": f"MediaBrowser Token=\"{api_key}\""} if api_key else {},
        )

    @property
    def pending(self) -> set[str]:
        with self._lock:
            return set(self._pending)

    def touch(self, rel_folder: str) -> None:
        if not self.api_key:
            return
        with self._lock:
            self._pending.add(rel_folder)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            self._loop = running
            self._ensure_task()
        elif self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._ensure_task)

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(self.debounce)
        await self.flush()

    async def flush(self) -> None:
        with self._lock:
            folders = sorted(self._pending)
            self._pending.clear()
        if not folders:
            return
        body = {"Updates": [{"Path": f"{self.library_path}/{f}", "UpdateType": "Created"}
                            for f in folders]}
        try:
            r = await self._client.post("/Library/Media/Updated", json=body)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - must never raise out of the pipeline
            logger.warning("Jellyfin: rescan notification failed (%s); keeping %d folder(s) pending",
                           e, len(folders))
            with self._lock:
                self._pending.update(folders)
            return
        logger.info("Jellyfin: notified %d updated folder(s)", len(folders))

    async def _virtual_folders(self) -> list[dict]:
        r = await self._client.get("/Library/VirtualFolders")
        r.raise_for_status()
        return r.json() or []

    def _has_path(self, vf: dict) -> bool:
        return any((loc or "").rstrip("/") == self.library_path for loc in vf.get("Locations") or [])

    async def test(self) -> dict:
        if not self.api_key:
            return {"ok": False, "detail": NO_KEY}
        try:
            r = await self._client.get("/System/Info")
            if r.status_code in (401, 403):
                return {"ok": False, "detail": f"API key rejected (HTTP {r.status_code})"}
            r.raise_for_status()
            info = r.json()
            folders = await self._virtual_folders()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "detail": f"Jellyfin unreachable: {e.__class__.__name__}: {e}"}
        exists = any(self._has_path(vf) for vf in folders)
        return {"ok": True,
                "detail": (f"Connected to {info.get('ServerName')} (Jellyfin {info.get('Version')}); "
                           f"library {'found' if exists else 'not created yet'}")}

    async def create_library(self) -> dict:
        """POST /Library/VirtualFolders (Home Videos) unless it already exists."""
        if not self.api_key:
            return {"ok": False, "detail": NO_KEY}
        try:
            folders = await self._virtual_folders()
            for vf in folders:
                if self._has_path(vf):
                    return {"ok": True, "detail": f"Library {vf.get('Name')!r} already uses {self.library_path}"}
            if any(vf.get("Name") == self.library_name for vf in folders):
                return {"ok": False,
                        "detail": f"A library named {self.library_name!r} exists with other paths"}
            r = await self._client.post(
                "/Library/VirtualFolders",
                params={"name": self.library_name, "collectionType": "homevideos",
                        "paths": [self.library_path], "refreshLibrary": "true"},
                # Realtime monitoring can't see changes on a network share; we notify instead.
                json={"LibraryOptions": {"EnableRealtimeMonitor": False}},
            )
            r.raise_for_status()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "detail": f"Jellyfin request failed: {e}"}
        logger.info("Jellyfin: created Home Videos library %r at %s", self.library_name, self.library_path)
        return {"ok": True, "detail": f"Created Home Videos library {self.library_name!r} at {self.library_path}"}

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()
