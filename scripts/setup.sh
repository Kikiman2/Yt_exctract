#!/bin/sh
# One-time host setup for yt-extract. Run from the repo root:  sh scripts/setup.sh
#
# - ./data/staging is where downloads are assembled before being moved into the library.
# - The library folder gets a sentinel file. The app refuses to write anywhere under
#   MEDIA_ROOT unless it exists, so a share that isn't mounted (or a stale bind mount)
#   can't turn into files silently written to the host's empty mount point.
# - .env is created from .env.example with a fresh SESSION_SECRET.
#
# MEDIA_ROOT is read from .env (default /srv/media). sudo is used only when the
# current user can't write there.
set -eu

cd "$(dirname "$0")/.."

MEDIA_ROOT=/srv/media
if [ -f .env ]; then
  v=$(sed -n 's/^MEDIA_ROOT=//p' .env | tail -n 1)
  [ -n "$v" ] && MEDIA_ROOT=$v
fi
SENTINEL="$MEDIA_ROOT/.yt-extract-library"

run() {
  echo "+ $*"
  "$@"
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then run "$@"; else run sudo "$@"; fi
}

echo "== data folders"
run mkdir -p data/staging

echo "== library at $MEDIA_ROOT"
if [ -w "$(dirname "$MEDIA_ROOT")" ] || [ -w "$MEDIA_ROOT" ]; then
  run mkdir -p "$MEDIA_ROOT"
  [ -e "$SENTINEL" ] || run touch "$SENTINEL"
else
  as_root mkdir -p "$MEDIA_ROOT"
  [ -e "$SENTINEL" ] || as_root touch "$SENTINEL"
fi

echo "== .env"
if [ ! -f .env ]; then
  run cp .env.example .env
  secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  # Not via run(): the secret shouldn't end up in terminal scrollback.
  sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$secret|" .env
  echo "+ generated SESSION_SECRET in .env"
  echo "Created .env: set ADMIN_PASSWORD (and MEDIA_ROOT, JELLYFIN_API_KEY) before starting;"
  echo "re-run this script after changing MEDIA_ROOT so the sentinel lands in the right folder."
fi

echo "done. Next: docker compose up -d --build"
