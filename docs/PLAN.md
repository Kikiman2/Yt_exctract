# yt_extract rewrite: plan and contracts

Goal: rebuild yt_extract from the ground up in the style of MangaLord
(`Kikiman2/MangaLord`): layered code (domain / ports / adapters / services), login-protected
dashboard, tests with in-memory fakes, health checks that pause the pipeline, events log,
sentinel-protected library, pinned sidecars, setup script, README.

Decisions taken with the owner (brainstorm, 2026-10-04):
- Rewrite, but the SQLite DB stays compatible (`app/db.py` migrates in place).
- Same login as MangaLord (ADMIN_USERNAME/ADMIN_PASSWORD/SESSION_SECRET, rate limit, CSRF).
- Integrations: Jellyfin rescan + create library, library sentinel, NFO/poster sidecars,
  health page + events log.
- yt-dlp floats (upgrade on container start + in-app upgrade button); bgutil sidecar pinned.
- UI: server pages + per-page JS over a JSON API.
- Extras: cookies upload/status, failed-download auto retry with backoff, playlists as
  subscription source, per-subscription overrides (quality, shorts, live, retention).
- Keep port 2000, `MEDIA_ROOT`, `./data/cookies.txt`, layout
  `<MEDIA_ROOT>/<download_subfolder>/<Channel>/<title> [id].mp4`.
- Delivery: commit in small steps on branch `claude/determined-meitner-ziw6yw`, push, no PR.
Unattended decisions are recorded in `docs/DECISIONS.md`.

## Layout and ownership (agents may only edit files they own)

```
app/config.py models.py ports.py db.py   FOUNDATION (done; change only via a note in DECISIONS.md)
app/services/contract.py                 FOUNDATION (JSON shapes)
tests/fakes.py                           FOUNDATION (test doubles; additive changes allowed by C)
app/domain/{naming,filters,urls,retention}.py + tests/unit/test_{naming,filters,urls,retention}.py   AGENT A
app/youtube/{client,cookies}.py + tests/unit/test_{ytclient,cookies}.py                            AGENT A
app/library/{writer,nfo,jellyfin}.py + tests/unit/test_{writer,nfo,jellyfin}.py                    AGENT B
app/services/{context,subscriptions,downloads,retention,settings,health,cookies,runtime}.py,
  app/scheduler.py, tests/conftest.py, tests/dev_fake_data.py, tests/unit/test_services_*.py       AGENT C
app/templates/*, app/static/*                                                                      AGENT D (frontend)
app/auth.py, app/main.py, app/routes/*, tests/unit/test_{auth,api}.py                              AGENT E (backend web)
Dockerfile docker-compose.yml docker-entrypoint.sh .dockerignore .env.example .gitignore
  scripts/setup.sh README.md .github/workflows/ci.yml                                              AGENT F (deploy/docs)
```

## Domain function contracts (Agent A, used by C and the fakes)

- `domain/naming.py`
  - `sanitize_name(name: str) -> str`: replace `\ / : * ? " < > |` and control chars with `_`,
    collapse whitespace, strip dots/spaces at the ends, empty -> `"Unknown"`. (Old behaviour
    replaced only the invalid chars; keep results identical for ordinary names.)
  - `video_file_name(title: str, video_id: str, ext: str = "mp4") -> str` -> `"<title> [<id>].<ext>"`,
    title sanitised and truncated so the whole name is <= 200 UTF-8 bytes.
  - `rel_folder(subfolder: str, channel_name: str | None) -> str` ->
    `"<sanitised subfolder>/<sanitised channel or 'Manual'>"`.
  - `sidecar_names(file_name: str) -> dict` -> `{"nfo": "<stem>.nfo", "poster": "<stem>-poster.jpg"}`.
- `domain/filters.py`
  - `skip_reason(info: dict, *, skip_shorts, shorts_max_seconds, skip_live, force) -> str | None`
    pure port of the old `_build_match_filter` (live_status is_live/is_upcoming; `media_type == "short"`
    authoritative, else `/shorts/` URL or duration <= max). `force` -> None.
- `domain/urls.py`
  - `classify_input(value: str) -> tuple[str, str]` -> `("channel_url"|"playlist_url"|"handle", normalised)`;
    bare `name` -> `https://www.youtube.com/@name`; playlist = URL with `list=`/`/playlist`.
  - `extract_video_id(url: str) -> str | None` for watch / youtu.be / shorts / live / embed URLs.
- `domain/retention.py`
  - `is_expired(row: dict, global_days: int, now_iso: str) -> bool`: row is a `db.list_downloaded()` row.
    Manual videos (`channel_id` NULL) never expire. Subscription with `sub_auto_delete == 0` never expires.
    Days = `sub_retention_days` if not NULL else `global_days`; days <= 0 never expires; expired when
    `downloaded_at` is older than `now - days`.
  - `backoff_hours(attempts: int, base_hours: int) -> int`: `base * 2**(attempts-1)`, capped at 7*24.

## youtube adapter (Agent A)

`app/youtube/client.py: class YtDlpClient` implements `ports.YouTubePort`.
Options come from `config` (cookie file if `config.COOKIES_FILE` exists, bgutil `extractor_args`
`{"youtubepot-bgutilhttp": {"base_url": [config.POT_PROVIDER_URL]}}`). Output template inside
`req.staging_dir`: `%(id)s.%(ext)s`, `merge_output_format="mp4"`, `writethumbnail` converted to jpg
if possible. Map yt-dlp `DownloadError` to `YouTubeError(msg, bot_check=("confirm you're not a bot" in msg))`.
`fetch_feed`: channels via `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (feedparser, via httpx or
feedparser's own fetch; a non-200 / bozo-without-entries raises YouTubeError); playlists via
`https://www.youtube.com/feeds/videos.xml?playlist_id=<id>` the same way. `upgrade()` runs
`sys.executable -m pip install --upgrade "yt-dlp[default]" bgutil-ytdlp-pot-provider` and returns the new version.
`app/youtube/cookies.py`: `parse_cookies(data: bytes) -> str` (accepts Netscape cookies.txt or the JSON
formats handled by the old `scripts/convert_cookies.py` (see git tag `pre-rewrite`); returns Netscape text;
raises `models.CookiesInvalid`), `inspect(path: Path) -> models.CookiesStatus` (counts, youtube login cookies
SID/__Secure-3PSID/LOGIN_INFO..., earliest expiry, mtime).
All network access is behind tiny seams (`_extract(opts, url)`, `_fetch_feed_text(url)`) that tests monkeypatch.
No test may touch the network.

## library adapter (Agent B)

- `library/writer.py: class LibraryWriter(root: Path, sentinel: str)`: see `ports.LibraryWriterPort`.
  Sentinel = file `<root>/<sentinel>`. `install` copies via `.<name>.part` + `os.replace` (src is then removed;
  works across filesystems). Path-traversal safe: rel paths are resolved and must stay under `root`.
- `library/nfo.py`: `render_nfo(meta: VideoMeta, channel: str | None) -> bytes` (Jellyfin `<movie>` NFO:
  title, plot, premiered/year, studio=channel, runtime in minutes, uniqueid type youtube, XML-escaped, no
  invalid control chars) and `poster_bytes(src: Path) -> bytes | None` (just reads the thumbnail).
- `library/jellyfin.py: class JellyfinNotifier(url, api_key, library_path, library_name)` implements
  `MediaServerNotifier` with httpx.AsyncClient: `touch()` records a folder; a debounce (default 10 s, `debounce`
  ctor arg so tests use 0) sends `POST /Library/Media/Updated` with `{"Updates":[{"Path": <library_path>/<folder>,
  "UpdateType":"Created"}]}` and header `X-Emby-Token`; `test()` hits `GET /System/Info` ->
  `{"ok","detail"}`; `create_library()` posts `/Library/VirtualFolders` with `collectionType=homevideos`,
  name, path (idempotent: already exists -> ok). Missing API key -> `{"ok": False, "detail": "JELLYFIN_API_KEY not set"}`,
  `touch()` is then a no-op. Look at MangaLord's `app/library/jellyfin.py` for the tested approach and reuse its style.

## services layer (Agent C), signatures the web layer relies on

All `async`. Services pull adapters from `services.context.ctx()` (`Context(youtube, writer, notifier)`).
Errors: `ValueError` (bad input -> 400), `KeyError` (unknown id -> 404), `models.YouTubeError` (-> 502),
`models.LibraryOffline` (-> 503).

```
services/subscriptions.py
  add_source(url) -> SubscriptionJson          # resolves, inserts (dup -> ValueError), logs event, refreshes it once
  list_sources() -> list[SubscriptionJson]
  update_source(source_id, fields: dict) -> SubscriptionJson   # validated: enabled, auto_delete_enabled (bool),
                                               # quality (None|key of QUALITY_FORMATS), skip_shorts/skip_live (None|bool),
                                               # retention_days (None|0..3650)
  remove_source(source_id) -> None
  refresh_one(source_id) -> int                # new videos queued; records last_checked_at / last_error
  refresh_all() -> int                         # scheduler job; one source failing never stops the rest
  add_video_by_url(url) -> VideoJson           # manual link: force=True; existing row -> requeued
  list_videos(status=None, source_id=None, limit=200, offset=0) -> list[VideoJson]
  retry_video(video_id, force=False) -> VideoJson
  delete_video(video_id) -> VideoJson          # delete file via writer, status 'deleted'
  check_missing() -> int                       # downloaded rows whose file is gone -> requeue
services/downloads.py
  start() / stop()                             # worker pool; stop() cancels cleanly
  set_concurrency(n) ; get_progress(video_id) -> ProgressJson | None
  set_health_reasons(list[str]) ; pause_reasons() -> list[str] ; pause() ; resume()  (pipeline_paused setting)
  queue_status() -> QueueJson
  retry_due() -> int                           # scheduler job: db.requeue_failed_due
  process_one(video: dict) -> None             # claim -> download into STAGING_DIR/<id> -> install -> sidecars ->
                                               # touch notifier -> mark downloaded; failure -> failed + attempts+1 +
                                               # next_retry_at (retention.backoff_hours); LibraryOffline -> back to
                                               # pending with no attempt consumed + health reason
services/retention.py   cleanup() -> int       # delete expired files (domain.retention.is_expired) + prune events,
                                               # stale staging dirs
services/settings.py    get_settings() -> SettingsJson ; update_settings(values) -> SettingsJson   (validated, ranges;
                        reschedules poll job, applies concurrency, pause flag)
services/health.py      status() -> HealthJson (cached) ; check_now() -> HealthJson ; list_events(limit, video_id) ;
                        jellyfin_test() ; jellyfin_create_library() ; upgrade_ytdlp() -> {"version", "note"}
services/cookies.py     status() -> CookiesJson ; save(data: bytes) -> CookiesJson ; delete() -> CookiesJson
services/runtime.py     startup() / shutdown()      # builds Context (real or DEV_FAKE fakes), health.check_now(),
                                                    # downloads.start(), scheduler.start()
app/scheduler.py        start(poll_minutes) / reschedule_poll(minutes) / shutdown(); jobs: refresh_all (+once at start),
                        retry_due (10 min), cleanup (6 h), health (5 min)
```
Pipeline pauses (no new claims) while `pause_reasons()` is non-empty: user pause, library offline, low disk
(`min_free_gb`). yt-dlp bot-check failures do NOT pause; they fail the video with a clear error and a hint.

## HTTP API (Agent E implements; Agent D's JS calls it). JSON in/out, all require login except /login, /healthz

```
GET  /api/status                      -> StatusJson
GET  /api/videos?status=&source_id=&limit=&offset=  -> [VideoJson]
POST /api/videos            {url}     -> VideoJson
POST /api/videos/{id}/retry {force?}  -> VideoJson
POST /api/videos/{id}/delete          -> VideoJson
POST /api/videos/check-missing        -> {"requeued": n}
GET  /api/sources                     -> [SubscriptionJson]
POST /api/sources           {url}     -> SubscriptionJson
PATCH /api/sources/{id}     {fields}  -> SubscriptionJson
DELETE /api/sources/{id}              -> {"ok": true}
POST /api/sources/{id}/refresh        -> {"new": n}
POST /api/refresh                     -> {"new": n}
POST /api/queue/pause | /api/queue/resume -> QueueJson
GET  /api/settings -> SettingsJson ;  PUT /api/settings {key: value,...} -> SettingsJson
GET  /api/health -> HealthJson ; POST /api/health/check -> HealthJson
GET  /api/events?limit=&video_id=     -> [EventJson]
POST /api/jellyfin/test | /api/jellyfin/create-library -> {"ok","detail"}
GET  /api/cookies -> CookiesJson ; POST /api/cookies (multipart field "file") -> CookiesJson ; DELETE /api/cookies -> CookiesJson
POST /api/ytdlp/upgrade               -> {"version","note"}
GET  /login  POST /login (form username,password,next)  POST /logout   GET /healthz -> {"ok": true}
Pages (login required): /  (dashboard) /sources /settings /health
```
Errors are `{"detail": "..."}` with 400/401/403/404/502/503. Mutations are fetch()-driven; the CSRF check
requires the browser's same-origin `Origin` header, which fetch sends automatically.

## Rules for every agent

- Python 3.11, venv at `/tmp/claude-0/venv` (`/tmp/claude-0/venv/bin/python -m pytest`). No network in tests.
- Edit only files you own. If a contract is wrong or missing, do NOT edit foundation files: put a short note in
  your final report (and in `docs/DECISIONS.md` only if you are Agent C or the lead).
- Match MangaLord style: module docstring saying what/why, comments explain *why*, no dead code, type hints.
- Write tests for everything you add; run the full suite before reporting (`pytest -q`); report the result honestly.
- Commit your own files in small commits (`git add <your files>`, never `git add -A`; never push; never rewrite
  history; if the index is locked by a sibling, wait a few seconds and retry). End commit messages with:
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_019bpPXEtHcYpa6dwNGE31BL`.
