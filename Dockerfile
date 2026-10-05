FROM python:3.12-slim

# ffmpeg merges video+audio streams and converts thumbnails; curl serves the
# HEALTHCHECK and the deno installer; unzip is needed by the deno installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: the JS runtime yt-dlp shells out to for solving YouTube's "n" signature
# challenge (without it, many formats silently disappear from extraction).
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV MEDIA_ROOT=/media \
    DATA_DIR=/app/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
# sh -c so ${PORT} is honoured; exec so uvicorn is PID 1's child under tini (init: true)
# and receives SIGTERM.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
