# yt_extract

Self-hosted YouTube auto-downloader with a web GUI, meant to be reached over Tailscale
and to drop videos into a folder Jellyfin can serve as a Home Videos library.

- Subscribe to channels by URL/handle; their newest uploads are detected via each
  channel's public RSS feed and downloaded automatically.
- Paste a single video link on the dashboard to download it on demand.
- Download subfolder, poll interval, and quality are all editable from Settings.

## Run

1. Copy `.env.example` to `.env` and set `MEDIA_ROOT` to the host directory you want
   videos saved under (e.g. your Jellyfin library root).
2. `docker compose up --build -d`
3. Open `http://<host>:2000/` (e.g. your Tailscale IP, port 2000).

The GUI's "download subfolder" setting (Settings page) is a folder name *inside*
`MEDIA_ROOT` — changing `MEDIA_ROOT` itself requires editing `.env` and restarting
the container, since Docker can't remount a new host path into a running container.

In Jellyfin, add a **Home Videos** library pointed at `MEDIA_ROOT` (or the specific
subfolder) to browse the downloaded videos.

### YouTube "Sign in to confirm you're not a bot"

This means YouTube is bot-checking the server's IP. Two things fix it, and it's
worth doing both:

1. **Cookies (main fix).** Export cookies from a logged-in browser session as a
   Netscape-format `cookies.txt` (e.g. via a browser extension like "Get
   cookies.txt LOCALLY") and drop the file at `./data/cookies.txt` on the host.
   It's picked up automatically on the next download/RSS check — no restart
   needed. Keep this file private; anyone with it can act as your logged-in
   YouTube session, and cookies expire periodically and need re-exporting.
2. **Keep yt-dlp current.** YouTube regularly changes its checks, and stale
   yt-dlp builds are the other common cause of this error re-appearing after
   working fine for a while. The container's entrypoint (`docker-entrypoint.sh`)
   upgrades `yt-dlp` to the latest release on every start, so restarting the
   container (`docker compose restart yt-extract`, or after a crash since it's
   `unless-stopped`) picks up any upstream fix without a rebuild.

## Development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MEDIA_ROOT=./media DATA_DIR=./data
uvicorn app.main:app --reload --port 8000
```

Requires `ffmpeg` on PATH for merging video/audio streams.
