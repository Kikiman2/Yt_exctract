# yt_extract

Self-hosted YouTube auto-downloader with a login-protected web dashboard, meant to be
reached over Tailscale (or a reverse proxy) and to drop videos into a folder Jellyfin
serves as a Home Videos library.

- Subscribe to **channels and playlists**; new uploads are detected through YouTube's
  public RSS feeds and downloaded automatically.
- Paste a single video link on the dashboard to download it on demand (it jumps the queue
  and ignores the shorts/live filters).
- Each video is saved as `<MEDIA_ROOT>/<subfolder>/<Channel>/<title> [id].mp4` with an
  `.nfo` and poster next to it, then Jellyfin is asked to rescan.
- Failed downloads retry on their own with exponential backoff; old videos are deleted
  after a retention period (global, overridable per source).
- A health page and events log explain why the pipeline is paused (library not mounted,
  low disk, Jellyfin unreachable, ...).

```
                         host:2000
 browser ──login──> yt-extract (FastAPI, root) ──PO tokens──> bgutil-provider:4416
                      │   │  └─ yt-dlp + ffmpeg + deno ──> youtube.com (RSS + downloads)
                      │   ├─ ./data           app.db (SQLite), cookies.txt, staging/
                      │   └─ /media = $MEDIA_ROOT   (local disk or NAS share)
                      │          └─ .yt-extract-library  (sentinel, must exist)
                      │          └─ <subfolder>/<Channel>/<title> [id].mp4 + .nfo + poster
                      └─ http://host.docker.internal:8096 ──> Jellyfin (rescan, create library)
```

Downloads are assembled in `data/staging/<id>/` and moved into the library only when
complete, so a slow or flaky share never holds a half-written video under its final name.

## Install

1. `sh scripts/setup.sh`
   - creates `./data/staging`;
   - creates `$MEDIA_ROOT` (read from `.env`, default `/srv/media`) with the sentinel file
     `.yt-extract-library` (uses sudo only if needed);
   - copies `.env.example` to `.env` with a fresh `SESSION_SECRET`.
2. Edit `.env`: set `ADMIN_PASSWORD`, `MEDIA_ROOT` (then re-run the script so the sentinel
   lands there) and, for Jellyfin, `JELLYFIN_API_KEY`. Every variable is documented in
   `.env.example`.
3. `docker compose up -d --build`
4. Open `http://<host>:2000/` and log in.

The app never writes to the library while the sentinel is missing. That is how an
unmounted share shows up: the pipeline pauses and the health page says so. After fixing
the mount, `docker compose restart yt-extract` (bind mounts can go stale when a share
reconnects).

If the dashboard is behind HTTPS, set `COOKIE_SECURE=true`, and list the proxy's address in
`TRUSTED_PROXIES` so the login rate limit sees real client IPs.

## First run

1. **Settings**: pick the download subfolder (a folder name *inside* `MEDIA_ROOT`),
   quality, poll interval, retention, retry policy and concurrency.
2. **Health**: "Test Jellyfin", then "Create library" (see Jellyfin below).
3. **Sources**: paste a channel URL, an `@handle`, a bare name or a playlist URL. The
   newest uploads are queued on the first refresh.

Changing `MEDIA_ROOT` itself means editing `.env` and recreating the container, since
Docker can't remount a new host path into a running one.

## Sources and playlists

A source is a channel (`https://www.youtube.com/@name`, `/channel/UC...`, or just `name`) or
a playlist (any URL with `list=`). Playlist sources download every new item that shows up
in the playlist. Removing a source keeps its already downloaded videos; they just stop
being auto-deleted by that source's rules.

Each source can override the global defaults: quality, skip shorts, skip live streams,
auto-delete on/off and retention days. Manual links bypass the shorts/live filters.

## Retention

Downloaded videos older than **auto-delete days** (default 30, `0` = never) are deleted
together with their sidecars. Per source you can set your own number of days, or turn
auto-delete off entirely for that source. Manually added videos never expire. Cleanup runs
every 6 hours and also prunes old events and stale staging folders.

## Retry and backoff

A failed download is retried automatically after `retry_failed_after_hours` (default 6),
doubling with every attempt (6 h, 12 h, 24 h, ... capped at 7 days), for at most
`max_attempts` attempts (default 5). After that it stays failed until you press Retry.
A library outage does not consume an attempt: the video goes back to pending and the
pipeline pauses until the library is back. Bot-check failures do not pause the pipeline;
they fail the video with a hint to refresh your cookies.

## YouTube "Sign in to confirm you're not a bot"

This means YouTube is bot-checking the server's IP. Two things fix it, and it's worth
doing both:

1. **Cookies (main fix).** Export cookies from a logged-in browser session (e.g. with the
   extension "Get cookies.txt LOCALLY") and upload the file on the **Settings** page. The app
   accepts Netscape `cookies.txt` as well as the JSON exports of Cookie-Editor,
   EditThisCookie and Playwright/Puppeteer, converts them and stores them as
   `./data/cookies.txt`. The page shows how many cookies, whether the login cookies are
   present and when the earliest one expires. They are picked up on the next download or
   feed check, no restart. Keep this file private: anyone with it can act as your logged-in
   YouTube session, and cookies expire periodically and need re-exporting.
   `scripts/convert_cookies.py` still works as a standalone CLI
   (`python3 scripts/convert_cookies.py exported.json data/cookies.txt`) but the upload
   supersedes it; you only need it if you want to convert on a machine without the app.
2. **Keep yt-dlp current.** YouTube regularly changes its checks, and stale yt-dlp builds
   are the other common cause of this error. The container's entrypoint upgrades `yt-dlp`
   to the latest release on every start (a failed upgrade only logs a warning), and the
   dashboard has an "Upgrade yt-dlp" button that does the same without a restart.

The bgutil sidecar supplies PO tokens so yt-dlp gets real formats instead of the SABR
storyboard-only response; deno solves the "n" signature challenge. Both are already in
the compose file and image.

## Jellyfin

1. Give Jellyfin read access to the library: mount `$MEDIA_ROOT` into its container (e.g.
   `${MEDIA_ROOT}:/data/YouTube:ro`) and set `JELLYFIN_LIBRARY_PATH` to that container path.
2. Jellyfin: Dashboard -> API Keys -> create one, put it in `.env` as `JELLYFIN_API_KEY`,
   then `docker compose up -d yt-extract`.
3. In the app's Health page: "Test Jellyfin", then "Create library". This creates a
   **Home Videos** library named `YouTube` (`JELLYFIN_LIBRARY_NAME`) pointing at
   `JELLYFIN_LIBRARY_PATH`. You can create it by hand instead: Home Videos, folder = the
   same path.
4. Each video gets `<name>.nfo` (title, description, upload date, channel as studio,
   runtime, YouTube id) and `<name>-poster.jpg`. Enable the NFO metadata reader for the
   library (it is on by default for Home Videos). `write_nfo` can be turned off in Settings.

After each install the app tells Jellyfin about just the changed folder (debounced), so
there is no full-library scan. If Jellyfin is unreachable from the container, set
`JELLYFIN_URL=http://172.17.0.1:8096`.

## Upgrading

- **App:** `git pull && docker compose up -d --build`.
- **yt-dlp:** restart (`docker compose restart yt-extract`) or use the dashboard button.
- **bgutil:** it is pinned in `docker-compose.yml` (`brainicism/bgutil-ytdlp-pot-provider`).
  Check Docker Hub for a newer tag, bump it, `docker compose up -d bgutil-provider`. The
  provider and the `bgutil-ytdlp-pot-provider` pip plugin (upgraded with yt-dlp) are
  versioned together; if PO tokens stop working after a bump, check both are on the same major.

## Migrating from the previous version

The rewrite keeps port 2000, `MEDIA_ROOT`, `./data` and the folder layout.

1. Back up `data/` (see Backup).
2. `git pull`; compare your `.env` with `.env.example`.
3. **New login variables:** `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SESSION_SECRET` are
   required now (the old app had no login). `sh scripts/setup.sh` leaves an existing `.env`
   alone; add them by hand, with a secret from
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
4. **Touch the sentinel:** `touch "$MEDIA_ROOT/.yt-extract-library"` (or run
   `sh scripts/setup.sh`). Until it exists downloads are paused and the health page says why.
5. `docker compose up -d --build`. `data/app.db` is migrated in place on first start
   (channels become sources, existing videos and settings are kept, `cookies.txt` is reused).
   Already downloaded files are not touched.

**Rolling back:** the original code is tagged `pre-rewrite`.
`git checkout pre-rewrite && docker compose up -d --build`. The old app ignores the new
columns, but restore your `data/app.db` backup if anything looks off, since the migration
is one-way.

## Backup

Everything that matters is under `./data` plus the library itself:

| Path | What | Needed? |
|---|---|---|
| `data/app.db` | sources, video states, settings, events | **yes**, source of truth |
| `data/cookies.txt` | your YouTube session cookies | convenient; re-export if lost (treat as a secret) |
| `data/staging/` | in-flight downloads | no |
| `.env` | login, secrets, Jellyfin key | yes |
| `$MEDIA_ROOT` | the videos | lives on your disk/NAS |

Consistent copy of the database while running:

```sh
docker compose exec yt-extract python -c "import sqlite3; s=sqlite3.connect('/app/data/app.db'); d=sqlite3.connect('/app/data/app.db.bak'); s.backup(d)"
```

Or stop the app (`docker compose stop yt-extract`) and copy `data/`.

## Troubleshooting

- **Pipeline paused.** Open the Health page; it lists the reasons: library sentinel
  missing, free space below `min_free_gb`, paused by hand.
- **Stale or unmounted share.** Fix the mount, then `docker compose restart yt-extract`.
- **Bot check / "Sign in to confirm".** See the section above: upload fresh cookies, restart
  to upgrade yt-dlp.
- **Only storyboard images / no formats.** The bgutil container is down or unreachable:
  `docker compose logs bgutil-provider`; check `POT_PROVIDER_URL`.
- **Jellyfin unreachable.** Use `JELLYFIN_URL=http://172.17.0.1:8096`, check the API key.
- **Can't log in behind a proxy / "too many attempts".** Set `TRUSTED_PROXIES`, and
  `COOKIE_SECURE=true` only when served over HTTPS.
- **Container shows unhealthy.** `docker inspect --format '{{json .State.Health}}' yt-extract`;
  the check is `GET /healthz`.
- **Logs:** `docker compose logs -f yt-extract`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # unit tests, no network
DEV_FAKE=1 ADMIN_USERNAME=admin ADMIN_PASSWORD=dev SESSION_SECRET=dev \
  DATA_DIR=./data/dev MEDIA_ROOT=./data/dev/media \
  .venv/bin/uvicorn app.main:app --reload --port 8000
```

`DEV_FAKE=1` wires in-memory fakes instead of yt-dlp and the library, so the whole UI works
offline. Real runs need `ffmpeg` on PATH. CI (`.github/workflows/ci.yml`) runs pytest on
Python 3.11 and 3.12 and builds the Docker image.
