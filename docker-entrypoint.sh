#!/bin/sh
set -e

# YouTube frequently changes its bot-detection/extraction internals; yt-dlp
# ships fixes for breakage like "Sign in to confirm you're not a bot" within
# days. Pull the latest release on every container start so a long-running
# container self-heals without requiring a manual image rebuild.
pip install --no-cache-dir --upgrade --quiet "yt-dlp[default]" bgutil-ytdlp-pot-provider \
  || echo "Warning: could not update yt-dlp, continuing with existing version" >&2

exec "$@"
