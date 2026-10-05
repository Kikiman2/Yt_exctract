# Decisions made without asking (review in the morning)

Each entry: what, why, how to revert. Newest last.

1. **Safety baseline.** Git tag `pre-rewrite` and branch `backup/pre-rewrite` point at the original code.
   Revert everything: `git reset --hard pre-rewrite` (on a scratch branch), or check out `backup/pre-rewrite`.
2. **Sentinel required by default.** The library root must contain `.yt-extract-library` before anything is written
   (as MangaLord does). Existing installs need a one-time `touch $MEDIA_ROOT/.yt-extract-library` (`scripts/setup.sh`
   does it). Until it exists, downloads pause and the health page says why. Why: you chose the sentinel feature;
   making it optional would defeat its purpose.
3. **Timestamps stay naive UTC ISO strings** (to the second) so rows written by the old app compare correctly.
4. **Subscription = row of the old `channels` table**, with new `kind` column (`channel`|`playlist`). Playlist ids
   (PL...) can't collide with channel ids (UC...). Removing a subscription keeps its videos (channel_id -> NULL).
5. **Manual links are downloaded first** (`force_download DESC` in the claim query), as an explicit request.
6. **Files are downloaded to `DATA_DIR/staging/<video_id>/` and moved into the library** (atomic `.part` + rename),
   like MangaLord, so a slow/flaky share never holds a half-written video under its final name.
7. **Retry policy:** failed downloads retry after `retry_failed_after_hours` (default 6) doubling per attempt,
   max `max_attempts` (default 5); then they stay failed until retried by hand.
8. **Bot-check failures do not pause the pipeline**; they fail the video with a hint to refresh cookies.
9. **`db.claim_next` no longer uses `UPDATE ... RETURNING`** (agent C). With the single shared aiosqlite connection,
   an open RETURNING cursor made a concurrent commit from another worker fail with "SQL statements in progress".
   It is now SELECT + conditional UPDATE (retries if another worker took the row). Same behaviour and signature.
10. **Revert point on GitHub is `origin/main`** (`7cbdcb7`, the original code). The local `pre-rewrite` tag could not be
    pushed (remote dropped the connection), so it exists only in the session sandbox. To roll back: `git checkout main`.
11. **Unresolvable channel/playlist input is a 400** (service pre-validates with `domain.urls.classify_input`); real
    upstream failures stay 502.
12. **Playlists use the public RSS feed** like channels (about 15 newest entries, no yt-dlp call). `ports.py` docstring
    corrected. Very long playlists therefore aren't back-filled; say so if you want a full back-fill option.
13. **`DEV_FAKE=1` needs `tests/` importable**; `.dockerignore` excludes it, so fake mode works from a repo checkout,
    not inside the production image. Intentional.

## Open items / not verified (be honest)

- **Docker image was never built** (no Docker daemon in the build sandbox). `docker compose config` validates; CI builds
  the image on GitHub. First thing to do: `docker compose up --build -d` and watch `docker compose logs -f`.
- **No real YouTube download was run** (sandbox has no YouTube access). yt-dlp calls are covered by unit tests with
  seams mocked; the options were ported from the old, working code. First real test: paste one video link.
- **bgutil pinned to `brainicism/bgutil-ytdlp-pot-provider:2.0.1`**: tag exists, same digest as `:latest` at the time,
  but its default port (compose assumes 4416, as the old config did) and plugin compatibility were not verified.
- **Rollback of a migrated DB to the old app is untested** (old app ignores the new columns; plausible, not proven).
  Back up `data/app.db` before the first start (README says so).
- Frontend verified with Chromium in `DEV_FAKE` mode (all 5 pages + mobile width, no JS errors), not against real data.
