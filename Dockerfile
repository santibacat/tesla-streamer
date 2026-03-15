# ── Stage 1: build yt-dlp binary ─────────────────────────────────────────────
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS base

# System deps: ffmpeg + curl (for yt-dlp download)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp as a standalone binary (always latest)
RUN curl -fsSL \
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux" \
    -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

# ── Stage 2: app ──────────────────────────────────────────────────────────────
FROM base AS app

WORKDIR /app

COPY server.py .

# Non-root user for security
RUN useradd -m -u 1000 streamer
RUN chown -R streamer:streamer /app \
    && chmod 755 /app \
    && chmod 644 /app/server.py
USER streamer

# ── Runtime config (all overridable via -e / docker-compose env) ──────────────
ENV HOST=0.0.0.0 \
    PORT=8080 \
    MJPEG_FPS=24 \
    FFMPEG_QUALITY=3 \
    STREAM_WIDTH=1920 \
    STREAM_HEIGHT=1080 \
    MAX_STREAMS=3

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:${PORT}/health || exit 1

CMD ["python3", "-u", "server.py"]
