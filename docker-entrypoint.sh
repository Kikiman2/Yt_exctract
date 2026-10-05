#!/bin/sh
set -e

DATA="${DATA_DIR:-/app/data}"
LIBRARY="${MEDIA_ROOT:-/media}"

# Downloads land here first and are moved into the library once complete.
mkdir -p "$DATA/staging"

# Don't abort startup: the app pauses its pipeline and the health page says why,
# which is more useful than a crash loop.
if [ ! -e "$LIBRARY/.yt-extract-library" ]; then
  echo "Warning: $LIBRARY/.yt-extract-library not found; the library share may not be mounted. Nothing will be written until it appears (see scripts/setup.sh)." >&2
fi

# YouTube frequently changes its bot-detection/extraction internals; yt-dlp
# ships fixes for breakage like "Sign in to confirm you're not a bot" within
# days. Pull the latest release on every container start so a long-running
# container self-heals without requiring a manual image rebuild.
pip install --no-cache-dir --upgrade --quiet "yt-dlp[default]" bgutil-ytdlp-pot-provider \
  || echo "Warning: could not update yt-dlp, continuing with existing version" >&2

exec "$@"
