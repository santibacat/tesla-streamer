#!/usr/bin/env python3
"""
Tesla MJPEG Streamer
Usage:
  http://yourserver/stream?url=https://youtube.com/watch?v=xxx
  http://yourserver/             → status page
  http://yourserver/health       → health check
"""

import subprocess
import threading
import time
import sys
import os
import signal
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("streamer")

# ── Config (override via env vars) ────────────────────────────────────────────
HOST          = os.environ.get("HOST", "0.0.0.0")
PORT          = int(os.environ.get("PORT", "8080"))
MJPEG_FPS     = int(os.environ.get("MJPEG_FPS", "24"))
FFMPEG_QUALITY= int(os.environ.get("FFMPEG_QUALITY", "3"))   # 1=best, 31=worst
STREAM_WIDTH  = int(os.environ.get("STREAM_WIDTH", "1920"))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", "1080"))
MAX_STREAMS   = int(os.environ.get("MAX_STREAMS", "3"))       # concurrent stream slots
AUDIO_DELAY_MS= int(os.environ.get("AUDIO_DELAY_MS", "0"))   # ms to delay video start after audio, to keep streams in sync
SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "/subscriptions.json")


# ── Per-stream state ──────────────────────────────────────────────────────────
class Stream:
    def __init__(self, stream_id: str, url: str, quality: int | None = None):
        self.id         = stream_id
        self.url        = url
        self.quality    = quality
        self.lock       = threading.Lock()
        self.frame      : bytes | None = None
        self.status     = "starting"   # starting | streaming | error | done
        self.title      = ""
        self.error      = ""
        self.error_detail = ""
        self.created_at = time.time()
        self.last_used  = time.time()
        self._yt_proc   = None
        self._ff_proc   = None
        self.started_at : float | None = None
        self.first_frame_at: float | None = None

    def stop(self):
        for proc in [self._ff_proc, self._yt_proc]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        self._ff_proc = None
        self._yt_proc = None

    def to_dict(self):
        return {
            "id":     self.id,
            "url":    self.url,
            "quality": self.quality,
            "started_at": self.started_at,
            "status": self.status,
            "title":  self.title,
            "error":  self.error,
            "error_detail": self.error_detail,
            "age_s":  round(time.time() - self.created_at),
        }


# ── Stream registry ───────────────────────────────────────────────────────────
class Registry:
    def __init__(self):
        self._lock    = threading.Lock()
        self._streams : dict[str, Stream] = {}
        self._counter = 0

    def _make_id(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    def get_or_create(
        self,
        url: str,
        quality: int | None = None,
        reuse_existing: bool = True,
    ) -> Stream:
        with self._lock:
            if reuse_existing:
                # Return existing live stream for same URL + quality profile
                for s in self._streams.values():
                    if (
                        s.url == url
                        and s.quality == quality
                        and s.status in ("starting", "streaming")
                    ):
                        s.last_used = time.time()
                        return s

            # Evict oldest if at capacity
            if len(self._streams) >= MAX_STREAMS:
                oldest = min(self._streams.values(), key=lambda s: s.last_used)
                log.info(f"Evicting stream {oldest.id} ({oldest.url[:60]})")
                oldest.stop()
                del self._streams[oldest.id]

            sid    = self._make_id()
            stream = Stream(sid, url, quality=quality)
            self._streams[sid] = stream
            return stream

    def get(self, sid: str) -> Stream | None:
        with self._lock:
            return self._streams.get(sid)

    def all_streams(self) -> list[Stream]:
        with self._lock:
            return list(self._streams.values())

    def cleanup_done(self):
        with self._lock:
            dead = [sid for sid, s in self._streams.items()
                    if s.status in ("error", "done")
                    and time.time() - s.last_used > 60]
            for sid in dead:
                self._streams[sid].stop()
                del self._streams[sid]
                log.info(f"Cleaned up stream {sid}")


registry = Registry()


# ── Pipeline ──────────────────────────────────────────────────────────────────
def fetch_title(stream: Stream):
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-playlist", "--print", "title", stream.url],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            with stream.lock:
                stream.title = r.stdout.strip()
    except Exception:
        pass


def run_pipeline(stream: Stream):
    log.info(f"[{stream.id}] Starting pipeline for: {stream.url[:80]}")
    threading.Thread(target=fetch_title, args=(stream,), daemon=True).start()

    try:
        def _format_candidates(quality: int | None) -> list[str]:
            if quality:
                q = quality
                return [
                    f"bestvideo[ext=mp4][height<={q}]/best[ext=mp4][height<={q}]",
                    f"bestvideo[height<={q}]/best[height<={q}]",
                    "bestvideo[ext=mp4]/best[ext=mp4]",
                    "bestvideo/best",
                ]
            return [
                "bestvideo[ext=mp4]/best[ext=mp4]",
                "bestvideo/best",
            ]

        def _drain_stderr(pipe, sink: list[str], max_chars: int = 4000):
            try:
                while True:
                    chunk = pipe.read(1024)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    sink.append(text)
                    current = sum(len(x) for x in sink)
                    if current > max_chars:
                        overflow = current - max_chars
                        while overflow > 0 and sink:
                            if len(sink[0]) <= overflow:
                                overflow -= len(sink[0])
                                sink.pop(0)
                            else:
                                sink[0] = sink[0][overflow:]
                                overflow = 0
            except Exception:
                pass

        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"
        attempt_errors: list[str] = []

        for attempt_idx, fmt in enumerate(_format_candidates(stream.quality), start=1):
            yt_cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", fmt,
                "-o", "-",
                "--quiet",
                stream.url,
            ]

            ff_cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-re",
                "-i", "pipe:0",
                "-vf", (
                    f"scale={STREAM_WIDTH}:{STREAM_HEIGHT}"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={STREAM_WIDTH}:{STREAM_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-fps_mode", "cfr",
                "-vcodec", "mjpeg",
                "-q:v", str(FFMPEG_QUALITY),
                "-r", str(MJPEG_FPS),
                "-f", "image2pipe",
                "-vframes", "99999999",
                "pipe:1",
            ]

            yt_stderr_chunks: list[str] = []
            ff_stderr_chunks: list[str] = []
            yt_proc = subprocess.Popen(
                yt_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ff_proc = subprocess.Popen(
                ff_cmd,
                stdin=yt_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            with stream.lock:
                stream._yt_proc = yt_proc
                stream._ff_proc = ff_proc
                stream.status = "streaming"
                if stream.started_at is None:
                    stream.started_at = time.time()

            yt_err_t = threading.Thread(
                target=_drain_stderr,
                args=(yt_proc.stderr, yt_stderr_chunks),
                daemon=True,
            )
            ff_err_t = threading.Thread(
                target=_drain_stderr,
                args=(ff_proc.stderr, ff_stderr_chunks),
                daemon=True,
            )
            yt_err_t.start()
            ff_err_t.start()

            log.info(f"[{stream.id}] Pipeline running (attempt {attempt_idx}, fmt={fmt})")

            frame_before = stream.frame
            buf = b""
            while True:
                chunk = ff_proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk

                while True:
                    start = buf.find(SOI)
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(EOI, start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]
                    with stream.lock:
                        stream.frame = frame
                        stream.last_used = time.time()
                        if stream.first_frame_at is None:
                            stream.first_frame_at = time.time()

            yt_rc = yt_proc.poll()
            ff_rc = ff_proc.poll()
            yt_err = "".join(yt_stderr_chunks).strip()
            ff_err = "".join(ff_stderr_chunks).strip()
            yt_err_t.join(timeout=0.2)
            ff_err_t.join(timeout=0.2)

            produced_frames = stream.frame is not None and stream.frame is not frame_before
            if produced_frames:
                with stream.lock:
                    stream.status = "done"
                log.info(f"[{stream.id}] Pipeline finished")
                break

            attempt_errors.append(
                f"attempt={attempt_idx} fmt={fmt} yt_rc={yt_rc} ff_rc={ff_rc} "
                f"yt_err={yt_err[-220:]} ff_err={ff_err[-220:]}"
            )
            for proc in (ff_proc, yt_proc):
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        else:
            with stream.lock:
                stream.status = "error"
                stream.error = "No video frames were produced"
                stream.error_detail = " || ".join(attempt_errors)[-1800:]
            log.error(f"[{stream.id}] Pipeline failed: {stream.error_detail}")

    except Exception as e:
        with stream.lock:
            stream.status = "error"
            stream.error  = str(e)
            stream.error_detail = ""
        log.error(f"[{stream.id}] Pipeline error: {e}")
    finally:
        stream.stop()


# ── HTML ──────────────────────────────────────────────────────────────────────
STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tesla Streamer</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@300;500&display=swap');
  :root{--red:#e31937;--dark:#090909;--panel:#111117;--border:#252530;--text:#e0e0ee;--muted:#555568;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--dark);color:var(--text);font-family:'Rajdhani',sans-serif;font-size:17px;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px;}
  h1{font-family:'Orbitron',monospace;font-weight:900;font-size:2rem;color:var(--red);letter-spacing:.12em;text-shadow:0 0 24px rgba(227,25,55,.45);margin-bottom:6px;}
  .sub{color:var(--muted);font-size:.9rem;letter-spacing:.08em;text-transform:uppercase;margin-bottom:28px;}
  .tabs{display:flex;gap:6px;margin-bottom:20px;width:100%;max-width:760px;}
  .tab-btn{font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;padding:8px 14px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s;}
  .tab-btn.active{background:var(--red);color:#fff;border-color:var(--red);}
  .tab-panel{display:none;width:100%;max-width:760px;}
  .tab-panel.active{display:block;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;width:100%;padding:28px 32px;margin-bottom:20px;}
  .card h2{font-family:'Orbitron',monospace;font-size:.85rem;letter-spacing:.15em;color:var(--muted);margin-bottom:18px;text-transform:uppercase;}
  .usage{font-family:monospace;font-size:.95rem;background:#0d0d14;border:1px solid var(--border);border-radius:6px;padding:14px 18px;line-height:1.9;word-break:break-all;}
  .usage span{color:var(--red);}
  .stream-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);}
  .stream-row:last-child{border-bottom:none;}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;letter-spacing:.06em;font-family:'Orbitron',monospace;}
  .badge.streaming{background:rgba(0,200,100,.15);color:#00c864;border:1px solid rgba(0,200,100,.3);}
  .badge.starting{background:rgba(255,152,0,.12);color:#ff9800;border:1px solid rgba(255,152,0,.3);}
  .badge.error{background:rgba(227,25,55,.12);color:var(--red);border:1px solid rgba(227,25,55,.3);}
  .badge.done{background:rgba(255,255,255,.05);color:var(--muted);border:1px solid var(--border);}
  .empty{color:var(--muted);font-size:.9rem;font-style:italic;}
  a{color:var(--red);text-decoration:none;}a:hover{text-decoration:underline;}
  .env-row{display:flex;gap:24px;flex-wrap:wrap;margin-top:4px;}
  .env-item{font-family:monospace;font-size:.85rem;color:var(--muted);}
  .env-item b{color:var(--text);}
  /* Feed tab */
  .feed-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:18px;}
  .feed-controls input{flex:1;min-width:220px;background:#0d0d14;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;font-size:.95rem;}
  .feed-controls button{background:var(--red);color:white;border:0;border-radius:6px;padding:10px 16px;font-family:'Orbitron',monospace;letter-spacing:.08em;cursor:pointer;white-space:nowrap;}
  .feed-status{color:var(--muted);font-size:.9rem;font-style:italic;margin-bottom:10px;min-height:1.4em;}
  .feed-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;}
  .feed-card{background:#0d0d14;border:1px solid var(--border);border-radius:8px;overflow:hidden;cursor:pointer;transition:border-color .15s;}
  .feed-card:hover{border-color:var(--red);}
  .feed-thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#1a1a24;display:block;}
  .feed-info{padding:8px 10px;}
  .feed-title{font-size:.85rem;line-height:1.3;color:var(--text);margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .feed-dur{font-family:monospace;font-size:.75rem;color:var(--muted);}
  /* shared input style for start-stream row */
  #yt-id{flex:1;min-width:280px;background:#0d0d14;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;}
  select{background:#0d0d14;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;}
</style>
</head>
<body>
<h1>TESLA STREAMER</h1>
<p class="sub">MJPEG video relay for Tesla browser</p>

<div class="tabs">
  <button class="tab-btn active" data-tab="stream">Stream</button>
  <button class="tab-btn" data-tab="feed">YouTube</button>
  <button class="tab-btn" data-tab="twitch">Twitch</button>
  <button class="tab-btn" data-tab="pluto">Pluto TV</button>
  <button class="tab-btn" data-tab="info">Info</button>
</div>

<!-- ── Stream tab ── -->
<div class="tab-panel active" id="tab-stream">
  <div class="card">
    <h2>Start stream</h2>
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:12px;">
      Paste any YouTube, Twitch, or X/Twitter video URL — or a YouTube video ID.
    </p>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <input id="yt-id" type="text" placeholder="URL or YouTube video ID">
      <select id="yt-quality">
        <option value="">Auto quality</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="360">360p</option>
        <option value="240">240p</option>
        <option value="144">144p</option>
      </select>
      <select id="yt-sync">
        <option value="0" selected>Video delay: 0 s (default)</option>
        <option value="500">Video delay: 0.5 s</option>
        <option value="1000">Video delay: 1 s</option>
        <option value="1500">Video delay: 1.5 s</option>
        <option value="2000">Video delay: 2 s</option>
        <option value="2500">Video delay: 2.5 s</option>
        <option value="3000">Video delay: 3 s</option>
      </select>
      <button id="go-stream"
              style="background:var(--red);color:white;border:0;border-radius:6px;padding:10px 16px;font-family:'Orbitron',monospace;letter-spacing:.08em;cursor:pointer;">
        OPEN STREAM
      </button>
    </div>
  </div>

  <div class="card">
    <h2>Active streams ({{stream_count}})</h2>
    {{streams_html}}
  </div>

  <div class="card">
    <h2>Configuration</h2>
    <div class="env-row">
      <div class="env-item">FPS <b>{{fps}}</b></div>
      <div class="env-item">Quality <b>{{quality}}</b></div>
      <div class="env-item">Resolution <b>{{width}}×{{height}}</b></div>
      <div class="env-item">Max streams <b>{{max_streams}}</b></div>
    <div class="env-item">Audio start delay <b>{{audio_delay_ms}} ms</b></div>
    <div class="env-item">Subscriptions <b>{{subs_status}}</b></div>
  </div>
</div>
</div>

<!-- ── Feed tab ── -->
<div class="tab-panel" id="tab-feed">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <select id="feed-quality">
        <option value="">Auto quality</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="360">360p</option>
        <option value="240">240p</option>
        <option value="144">144p</option>
      </select>
      <select id="feed-sync">
        <option value="0" selected>Delay: 0 s</option>
        <option value="500">Delay: 0.5 s</option>
        <option value="1000">Delay: 1 s</option>
        <option value="1500">Delay: 1.5 s</option>
        <option value="2000">Delay: 2 s</option>
        <option value="2500">Delay: 2.5 s</option>
        <option value="3000">Delay: 3 s</option>
      </select>
    </div>
  </div>

  <!-- Subscriptions panel (shown when cookies available) -->
  <div class="card" id="subs-card" style="display:none;">
    <h2>My subscriptions</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px;">
      <button id="subs-load" style="background:var(--red);color:white;border:0;border-radius:6px;padding:8px 16px;font-family:'Orbitron',monospace;letter-spacing:.08em;cursor:pointer;">LOAD SUBSCRIPTIONS</button>
      <input id="subs-filter" type="text" placeholder="Filter channels…"
             style="flex:1;min-width:180px;background:#0d0d14;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;display:none;">
    </div>
    <div class="feed-status" id="subs-status"></div>
    <div id="subs-list" style="display:flex;flex-direction:column;gap:0;"></div>
  </div>

  <!-- Manual channel lookup -->
  <div class="card">
    <h2 id="feed-card-title">Channel recent uploads</h2>
    <div class="feed-controls">
      <input id="feed-channel" type="text" placeholder="@channelhandle or channel URL">
      <button id="feed-go">LOAD FEED</button>
    </div>
    <div class="feed-status" id="feed-status"></div>
    <div class="feed-grid" id="feed-grid"></div>
  </div>
</div>

<!-- ── Twitch tab ── -->
<div class="tab-panel" id="tab-twitch">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <select id="twitch-quality">
        <option value="">Auto quality</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="360">360p</option>
        <option value="240">240p</option>
      </select>
      <select id="twitch-sync">
        <option value="0" selected>Delay: 0 s</option>
        <option value="500">Delay: 0.5 s</option>
        <option value="1000">Delay: 1 s</option>
        <option value="1500">Delay: 1.5 s</option>
        <option value="2000">Delay: 2 s</option>
      </select>
    </div>
  </div>
  <div class="card">
    <h2>Live stream</h2>
    <div class="feed-controls">
      <input id="twitch-live-channel" type="text" placeholder="channel name (e.g. xqc)">
      <button id="twitch-live-go">WATCH LIVE</button>
    </div>
  </div>
  <div class="card">
    <h2>VODs</h2>
    <div class="feed-controls">
      <input id="twitch-vod-channel" type="text" placeholder="channel name">
      <button id="twitch-vod-go">LOAD VODS</button>
    </div>
    <div class="feed-status" id="twitch-vod-status"></div>
    <div class="feed-grid" id="twitch-vod-grid"></div>
  </div>
</div>

<!-- ── Pluto TV tab ── -->
<div class="tab-panel" id="tab-pluto">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <select id="pluto-sync">
        <option value="0" selected>Delay: 0 s</option>
        <option value="500">Delay: 0.5 s</option>
        <option value="1000">Delay: 1 s</option>
        <option value="1500">Delay: 1.5 s</option>
        <option value="2000">Delay: 2 s</option>
      </select>
      <input id="pluto-filter" type="text" placeholder="Filter channels…"
             style="flex:1;min-width:180px;background:#0d0d14;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;">
    </div>
  </div>
  <div class="card">
    <h2>Channels</h2>
    <div class="feed-status" id="pluto-status"></div>
    <div id="pluto-list"></div>
  </div>
</div>

<!-- ── Info tab ── -->
<div class="tab-panel" id="tab-info">
  <div class="card">
    <h2>API usage</h2>
    <div class="usage">
      GET /watch<span>?url=</span>https://youtube.com/watch?v=VIDEO_ID<br>
      GET /watch<span>?url=</span>https://www.twitch.tv/CHANNEL<br>
      GET /watch<span>?url=</span>https://x.com/user/status/ID<br>
      GET /watch<span>?url=</span>https://…<span>&amp;quality=720&amp;sync=1000</span><br>
      GET /feed<span>?channel=</span>@handle<span>&amp;limit=12</span>  → JSON video list<br>
      GET /subscriptions  → JSON channel list<br>
      GET /health   → JSON health check<br>
      GET /status   → JSON active streams
    </div>
  </div>
</div>

<script>
(function () {
  // ── Tab switching ──
  var tabBtns = document.querySelectorAll(".tab-btn");
  var tabPanels = document.querySelectorAll(".tab-panel");
  tabBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = btn.getAttribute("data-tab");
      tabBtns.forEach(function (b) { b.classList.remove("active"); });
      tabPanels.forEach(function (p) { p.classList.remove("active"); });
      btn.classList.add("active");
      document.getElementById("tab-" + target).classList.add("active");
    });
  });

  // ── Stream tab ──
  var idInput    = document.getElementById("yt-id");
  var qualitySel = document.getElementById("yt-quality");
  var syncSel    = document.getElementById("yt-sync");
  var goButton   = document.getElementById("go-stream");
  syncSel.value  = "{{audio_delay_ms}}";

  function buildWatchUrl(videoUrl, quality, sync) {
    var target = "/watch?url=" + encodeURIComponent(videoUrl);
    if (quality) target += "&quality=" + encodeURIComponent(quality);
    if (sync)    target += "&sync="    + encodeURIComponent(sync);
    return target;
  }

  function resolveInputUrl(raw) {
    // Full URL (YouTube, Twitch, X/Twitter, etc.) — pass through
    if (/^https?:\/\//i.test(raw)) return raw;
    // Bare YouTube video ID (11 alphanum chars)
    if (/^[A-Za-z0-9_-]{11}$/.test(raw)) {
      return "https://www.youtube.com/watch?v=" + raw;
    }
    // Twitch channel shorthand: twitch:channel
    if (/^twitch:/i.test(raw)) {
      return "https://www.twitch.tv/" + raw.slice(7);
    }
    // Fallback: treat as YouTube ID anyway
    return "https://www.youtube.com/watch?v=" + raw;
  }

  function openStream() {
    var raw = (idInput.value || "").trim();
    if (!raw) { idInput.focus(); return; }
    window.location.href = buildWatchUrl(
      resolveInputUrl(raw), qualitySel.value, syncSel.value
    );
  }

  goButton.addEventListener("click", openStream);
  idInput.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) openStream();
  });

  // ── Twitch tab ──
  var twitchQuality  = document.getElementById("twitch-quality");
  var twitchSync     = document.getElementById("twitch-sync");
  var twitchLiveCh   = document.getElementById("twitch-live-channel");
  var twitchLiveGo   = document.getElementById("twitch-live-go");
  var twitchVodCh    = document.getElementById("twitch-vod-channel");
  var twitchVodGo    = document.getElementById("twitch-vod-go");
  var twitchVodSt    = document.getElementById("twitch-vod-status");
  var twitchVodGrid  = document.getElementById("twitch-vod-grid");
  twitchSync.value   = "{{audio_delay_ms}}";

  twitchLiveGo.addEventListener("click", function () {
    var ch = (twitchLiveCh.value || "").trim().replace(/^@/, "");
    if (!ch) { twitchLiveCh.focus(); return; }
    var url = "https://www.twitch.tv/" + ch;
    window.location.href = buildWatchUrl(url, twitchQuality.value, twitchSync.value);
  });
  twitchLiveCh.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) twitchLiveGo.click();
  });

  twitchVodGo.addEventListener("click", function () {
    var ch = (twitchVodCh.value || "").trim().replace(/^@/, "");
    if (!ch) { twitchVodCh.focus(); return; }
    twitchVodSt.textContent = "Loading VODs…";
    twitchVodGrid.innerHTML = "";
    var url = "https://www.twitch.tv/" + ch + "/videos";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/feed?channel=" + encodeURIComponent(url) + "&limit=12", true);
    xhr.timeout = 30000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        twitchVodSt.textContent = "Failed to parse response."; return;
      }
      if (data.error) { twitchVodSt.textContent = "Error: " + data.error; return; }
      var videos = data.videos || [];
      if (!videos.length) { twitchVodSt.textContent = "No VODs found."; return; }
      twitchVodSt.textContent = videos.length + " VODs";
      videos.forEach(function (v) {
        var card = document.createElement("div");
        card.className = "feed-card";
        var dur = fmtDuration(v.duration);
        card.innerHTML =
          '<img class="feed-thumb" src="' + (v.thumb || "") + '" loading="lazy" alt="">' +
          '<div class="feed-info">' +
          '<div class="feed-title">' + escHtml(v.title) + '</div>' +
          (dur ? '<div class="feed-dur">' + escHtml(dur) + '</div>' : '') +
          '</div>';
        card.addEventListener("click", function () {
          window.location.href = buildWatchUrl(
            v.url, twitchQuality.value, twitchSync.value
          );
        });
        twitchVodGrid.appendChild(card);
      });
    };
    xhr.send();
  });
  twitchVodCh.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) twitchVodGo.click();
  });

  // ── Pluto TV tab ──
  var plutoSync   = document.getElementById("pluto-sync");
  var plutoFilter = document.getElementById("pluto-filter");
  var plutoStatus = document.getElementById("pluto-status");
  var plutoList   = document.getElementById("pluto-list");
  plutoSync.value = "{{audio_delay_ms}}";

  // Public Pluto TV HLS streams (US, no account required)
  var PLUTO_CHANNELS = [
    {name:"CNN",             url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5e3c73cd2e5d2a0007c14f8d/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Fox News",        url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5e3c73cd2e5d2a0007c14f90/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"MSNBC",           url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/636418f0d8e6060007750ef7/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Bloomberg",       url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5cf29ac6197def0009486a49/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Sky News",        url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5e3ce9e5e0878800079e1ac2/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Action Movies",   url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f57b5b191be29e00e8718/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Comedy Central",  url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a234a494a7d1ac51fee/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Star Trek",       url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a9e4a494a7d1ac52155/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Crime Drama",     url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f58eab1974eca3aeb93e5/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Paranormal",      url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f58634a494a7d1ac51e86/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Drama Movies",    url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a154a494a7d1ac51fe3/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Horror",          url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f58d6b1974eca3aeb939f/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Science Fiction", url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f59834a494a7d1ac51ef4/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Anime",           url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a8a4a494a7d1ac52148/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Classic TV",      url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f57b5b191be29e00e874e/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Sports",          url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a364a494a7d1ac51ff9/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Latino Mix",      url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5cb5bace98e48e0009ae7bb7/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Music Videos",    url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f5a524a494a7d1ac52017/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Documentary",     url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f59044a494a7d1ac51ec1/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
    {name:"Nature",          url:"https://service-stitcher.clusters.pluto.tv/v2/stitch/embed/channel/5a3f59fa4a494a7d1ac51f32/master.m3u8?deviceId=teslastreamer&deviceType=web&sid=teslastreamer"},
  ];

  var filteredPluto = PLUTO_CHANNELS.slice();

  function renderPluto(channels) {
    plutoList.innerHTML = "";
    channels.forEach(function (ch) {
      var row = document.createElement("div");
      row.className = "stream-row";
      row.style.cursor = "pointer";
      row.innerHTML =
        '<span style="font-size:.95rem;">' + escHtml(ch.name) + '</span>' +
        '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);">LIVE →</span>';
      row.addEventListener("click", function () {
        window.location.href = buildWatchUrl(ch.url, "", plutoSync.value);
      });
      plutoList.appendChild(row);
    });
    plutoStatus.textContent = channels.length + " channels (US, no account required)";
  }

  renderPluto(PLUTO_CHANNELS);

  plutoFilter.addEventListener("input", function () {
    var q = (plutoFilter.value || "").toLowerCase().trim();
    filteredPluto = q
      ? PLUTO_CHANNELS.filter(function (c) {
          return c.name.toLowerCase().indexOf(q) !== -1;
        })
      : PLUTO_CHANNELS.slice();
    renderPluto(filteredPluto);
  });

  // ── Feed tab ──
  var feedChannel  = document.getElementById("feed-channel");
  var feedQuality  = document.getElementById("feed-quality");
  var feedSync     = document.getElementById("feed-sync");
  var feedGoBtn    = document.getElementById("feed-go");
  var feedStatus   = document.getElementById("feed-status");
  var feedGrid     = document.getElementById("feed-grid");
  var feedCardTitle= document.getElementById("feed-card-title");
  feedSync.value   = "{{audio_delay_ms}}";

  // Subscriptions
  var subsCard   = document.getElementById("subs-card");
  var subsLoad   = document.getElementById("subs-load");
  var subsFilter = document.getElementById("subs-filter");
  var subsStatus = document.getElementById("subs-status");
  var subsList   = document.getElementById("subs-list");
  var allChannels = [];

  // Probe whether subscriptions.json is mounted; show panel if so
  (function probeSubscriptions() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/subscriptions", true);
    xhr.timeout = 2000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status !== 503) {
        subsCard.style.display = "block";
      }
    };
    xhr.send();
  })();

  function renderChannelList(channels) {
    subsList.innerHTML = "";
    channels.forEach(function (ch) {
      var row = document.createElement("div");
      row.className = "stream-row";
      row.style.cursor = "pointer";
      row.innerHTML = '<a style="color:var(--text);font-size:.95rem;">' + escHtml(ch.name) + '</a>' +
                      '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);">LOAD →</span>';
      row.addEventListener("click", function () {
        feedChannel.value = ch.url;
        feedCardTitle.textContent = ch.name + " — recent uploads";
        loadFeed();
        // Scroll to feed card
        feedGrid.scrollIntoView({behavior: "smooth", block: "start"});
      });
      subsList.appendChild(row);
    });
  }

  function applyFilter() {
    var q = (subsFilter.value || "").toLowerCase().trim();
    if (!q) { renderChannelList(allChannels); return; }
    renderChannelList(allChannels.filter(function (ch) {
      return ch.name.toLowerCase().indexOf(q) !== -1;
    }));
  }

  subsLoad.addEventListener("click", function () {
    subsStatus.textContent = "Loading subscriptions…";
    subsList.innerHTML = "";
    subsFilter.style.display = "none";
    subsLoad.disabled = true;
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/subscriptions", true);
    xhr.timeout = 45000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      subsLoad.disabled = false;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        subsStatus.textContent = "Failed to parse response."; return;
      }
      if (data.error) {
        subsStatus.textContent = "Error: " + data.error; return;
      }
      allChannels = data.channels || [];
      if (!allChannels.length) {
        subsStatus.textContent = "No subscriptions found."; return;
      }
      var syncedAt = data.synced_at ? " · synced " + data.synced_at.slice(0, 10) : "";
      subsStatus.textContent = allChannels.length + " channels" + syncedAt;
      subsFilter.style.display = "";
      subsFilter.value = "";
      renderChannelList(allChannels);
    };
    xhr.send();
  });

  subsFilter.addEventListener("input", applyFilter);

  function fmtDuration(secs) {
    var s = parseInt(secs, 10);
    if (!s || isNaN(s)) return "";
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    if (h > 0) return h + ":" + pad(m) + ":" + pad(sec);
    return m + ":" + pad(sec);
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function loadFeed() {
    var ch = (feedChannel.value || "").trim();
    if (!ch) { feedChannel.focus(); return; }
    feedStatus.textContent = "Loading…";
    feedGrid.innerHTML = "";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/feed?channel=" + encodeURIComponent(ch) + "&limit=12", true);
    xhr.timeout = 30000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status < 200 || xhr.status >= 300) {
        feedStatus.textContent = "Error: " + xhr.status;
        return;
      }
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        feedStatus.textContent = "Failed to parse response.";
        return;
      }
      if (data.error) {
        feedStatus.textContent = "Error: " + data.error;
        return;
      }
      var videos = data.videos || [];
      if (!videos.length) {
        feedStatus.textContent = "No videos found for that channel.";
        return;
      }
      feedStatus.textContent = videos.length + " recent videos";
      feedGrid.innerHTML = "";
      videos.forEach(function (v) {
        var card = document.createElement("div");
        card.className = "feed-card";
        var dur = fmtDuration(v.duration);
        card.innerHTML =
          '<img class="feed-thumb" src="' + (v.thumb || "") + '" loading="lazy" alt="">' +
          '<div class="feed-info">' +
          '<div class="feed-title">' + escHtml(v.title) + '</div>' +
          (dur ? '<div class="feed-dur">' + escHtml(dur) + '</div>' : '') +
          '</div>';
        card.addEventListener("click", function () {
          window.location.href = buildWatchUrl(v.url, feedQuality.value, feedSync.value);
        });
        feedGrid.appendChild(card);
      });
    };
    xhr.send();
  }

  function escHtml(s) {
    return (s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  feedGoBtn.addEventListener("click", loadFeed);
  feedChannel.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) loadFeed();
  });
})();
</script>
</body></html>"""

WATCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tesla Stream Watch</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@300;500&display=swap');
  :root{--red:#e31937;--dark:#090909;--panel:#111117;--border:#252530;--text:#e0e0ee;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--dark);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:16px;}
  .top{width:100%;max-width:1280px;display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:12px;flex-wrap:wrap;}
  .title{font-family:'Orbitron',monospace;letter-spacing:.1em;color:var(--red);font-size:1rem;}
  .back{color:var(--red);text-decoration:none;font-family:monospace;}
  .wrap{width:100%;max-width:1280px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px;}
  img{width:100%;height:auto;display:block;background:black;border-radius:8px;}
  audio{width:100%;margin-top:10px;}
  .diag{margin-top:10px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:.85rem;line-height:1.4;white-space:pre-wrap;color:#f0b5bf;background:#160d11;display:none;}
</style>
</head>
<body>
  <div class="top">
    <div class="title">MJPEG + AUDIO</div>
    <a class="back" href="/">← Back</a>
  </div>
  <div class="wrap">
    <img id="mjpeg" alt="Live MJPEG stream">
    <audio id="audio" controls autoplay playsinline></audio>
    <div id="diag" class="diag"></div>
  </div>
<script>
(function () {
  var sid = "{{stream_id}}";
  var syncMs = "{{sync_ms}}";
  if (!sid) {
    window.location.href = "/";
    return;
  }
  var q = "?sid=" + encodeURIComponent(sid) + "&sync=" + encodeURIComponent(syncMs);
  var img = document.getElementById("mjpeg");
  var audio = document.getElementById("audio");
  var diag = document.getElementById("diag");

  // Start audio first so its pipeline is already running and buffered.
  audio.src = "/audio?sid=" + encodeURIComponent(sid);
  try {
    var p = audio.play();
    if (p && typeof p.catch === "function") p.catch(function () {});
  } catch (e) {}

  // Start video after sync_ms so audio has a head start equal to the video
  // pipeline's startup lag, keeping them in sync when the first frame appears.
  setTimeout(function () {
    img.src = "/stream" + q;
  }, parseInt(syncMs, 10) || 0);

  function showDiag(message) {
    diag.style.display = "block";
    diag.textContent = message;
  }

  img.addEventListener("error", function () {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/stream_status?sid=" + encodeURIComponent(sid), true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status < 200 || xhr.status >= 300) {
        showDiag("Video stream failed to load and diagnostics request failed.");
        return;
      }
      try {
        var data = JSON.parse(xhr.responseText);
        var msg = [
          "Video stream failed to load.",
          "status: " + (data.status || "unknown"),
          "error: " + (data.error || "n/a"),
          "detail: " + (data.error_detail || "n/a")
        ].join("\\n");
        showDiag(msg);
      } catch (err) {
        showDiag("Video stream failed to load and diagnostics parse failed.");
      }
    };
    xhr.send();
  });
})();
</script>
</body></html>"""


def render_status_page() -> str:
    streams = registry.all_streams()
    if not streams:
        streams_html = '<p class="empty">No active streams</p>'
    else:
        rows = []
        for s in sorted(streams, key=lambda x: x.created_at, reverse=True):
            title = s.title or s.url[:60] + "…"
            stream_url = f"/watch?url={quote(s.url, safe='')}"
            quality_tag = ""
            if s.quality:
                stream_url += f"&quality={s.quality}"
                quality_tag = f" · {s.quality}p"
            rows.append(
                f'<div class="stream-row">'
                f'<div><a href="{stream_url}">{title}{quality_tag}</a></div>'
                f'<span class="badge {s.status}">{s.status.upper()}</span>'
                f'</div>'
            )
        streams_html = "\n".join(rows)

    subs_status = "loaded" if os.path.isfile(SUBSCRIPTIONS_FILE) else "not mounted"
    return (STATUS_HTML
            .replace("{{stream_count}}", str(len(streams)))
            .replace("{{streams_html}}", streams_html)
            .replace("{{fps}}", str(MJPEG_FPS))
            .replace("{{quality}}", str(FFMPEG_QUALITY))
            .replace("{{width}}", str(STREAM_WIDTH))
            .replace("{{height}}", str(STREAM_HEIGHT))
            .replace("{{max_streams}}", str(MAX_STREAMS))
            .replace("{{audio_delay_ms}}", str(AUDIO_DELAY_MS))
            .replace("{{subs_status}}", subs_status))

def render_watch_page(stream_id: str, sync_ms: int) -> str:
    return (WATCH_HTML
            .replace("{{stream_id}}", stream_id)
            .replace("{{sync_ms}}", str(sync_ms)))


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt % args)

    @staticmethod
    def _safe_header_value(value: str) -> str:
        # http.server writes headers as latin-1; replace unsupported chars so
        # titles with unicode punctuation/emojis do not crash the request.
        cleaned = (value or "").replace("\r", " ").replace("\n", " ")
        return cleaned.encode("latin-1", "replace").decode("latin-1")

    @staticmethod
    def _parse_quality(raw_quality: str | None) -> int | None:
        if raw_quality is None or raw_quality == "":
            return None
        try:
            quality = int(raw_quality)
        except ValueError:
            raise ValueError("quality must be one of: 144,240,360,480,720,1080")
        if quality not in {144, 240, 360, 480, 720, 1080}:
            raise ValueError("quality must be one of: 144,240,360,480,720,1080")
        return quality

    @staticmethod
    def _parse_sync_ms(raw_sync: str | None) -> int:
        if raw_sync is None or raw_sync == "":
            return AUDIO_DELAY_MS
        try:
            sync_ms = int(raw_sync)
        except ValueError:
            raise ValueError("sync must be an integer milliseconds value")
        if sync_ms < 0 or sync_ms > 10000:
            raise ValueError("sync must be between 0 and 10000 milliseconds")
        return sync_ms

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/":
            html = render_status_page()
            self._html(html)

        elif path == "/health":
            self._json({"ok": True, "streams": len(registry.all_streams())})

        elif path == "/status":
            data = [s.to_dict() for s in registry.all_streams()]
            self._json({"streams": data})

        elif path == "/feed":
            channel = qs.get("channel", [None])[0]
            if not channel:
                self._error(400, "Missing ?channel= parameter")
                return
            limit = 12
            try:
                raw_limit = qs.get("limit", [None])[0]
                if raw_limit:
                    limit = max(1, min(int(raw_limit), 50))
            except (ValueError, TypeError):
                pass
            self._serve_feed(channel.strip(), limit)

        elif path == "/subscriptions":
            self._serve_subscriptions()

        elif path == "/stream_status":
            sid = qs.get("sid", [None])[0]
            if not sid:
                self._error(400, "Missing ?sid= parameter")
                return
            stream = registry.get(sid)
            if stream is None:
                self._error(404, "Stream session not found")
                return
            self._json(stream.to_dict())

        elif path == "/watch":
            raw_url = qs.get("url", [None])[0]
            if not raw_url:
                self._error(400, "Missing ?url= parameter")
                return
            raw_quality = qs.get("quality", [None])[0]
            try:
                quality = self._parse_quality(raw_quality)
            except ValueError as e:
                self._error(400, str(e))
                return
            raw_sync = qs.get("sync", [None])[0]
            try:
                sync_ms = self._parse_sync_ms(raw_sync)
            except ValueError as e:
                self._error(400, str(e))
                return
            video_url = unquote(raw_url)
            registry.cleanup_done()
            stream = registry.get_or_create(
                video_url,
                quality=quality,
                reuse_existing=False,
            )
            self._html(render_watch_page(stream.id, sync_ms))

        elif path == "/stream":
            sid = qs.get("sid", [None])[0]
            stream = None
            if sid:
                stream = registry.get(sid)
                if stream is None:
                    self._error(404, "Stream session not found")
                    return
            else:
                raw_url = qs.get("url", [None])[0]
                if not raw_url:
                    self._error(400, "Missing ?url= parameter")
                    return
                raw_quality = qs.get("quality", [None])[0]
                try:
                    quality = self._parse_quality(raw_quality)
                except ValueError as e:
                    self._error(400, str(e))
                    return
                video_url = unquote(raw_url)
                stream = registry.get_or_create(video_url, quality=quality)
            self._serve_mjpeg(stream)

        elif path == "/audio":
            raw_sync = qs.get("sync", [None])[0]
            try:
                sync_ms = self._parse_sync_ms(raw_sync)
            except ValueError as e:
                self._error(400, str(e))
                return
            sid = qs.get("sid", [None])[0]
            stream = None
            if sid:
                stream = registry.get(sid)
                if stream is None:
                    self._error(404, "Stream session not found")
                    return
            else:
                raw_url = qs.get("url", [None])[0]
                if not raw_url:
                    self._error(400, "Missing ?url= parameter")
                    return
                raw_quality = qs.get("quality", [None])[0]
                try:
                    quality = self._parse_quality(raw_quality)
                except ValueError as e:
                    self._error(400, str(e))
                    return
                video_url = unquote(raw_url)
                stream = registry.get_or_create(video_url, quality=quality)
            self._serve_audio(stream, sync_ms=sync_ms)

        else:
            self._error(404, "Not found")

    # ── Subscriptions ─────────────────────────────────────────────────────────
    def _serve_subscriptions(self):
        if not os.path.isfile(SUBSCRIPTIONS_FILE):
            self._error(503, f"Subscriptions file not found at {SUBSCRIPTIONS_FILE}. "
                            "Run sync_subscriptions.py and mount the resulting JSON.")
            return
        try:
            with open(SUBSCRIPTIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._error(500, f"Failed to read subscriptions file: {e}")
            return
        self._json({
            "synced_at": data.get("synced_at", ""),
            "channels":  data.get("channels", []),
        })

    # ── Feed ──────────────────────────────────────────────────────────────────
    def _serve_feed(self, channel: str, limit: int):
        # Normalise: bare handle (@channel), channel URL, or plain name
        if channel.startswith("http://") or channel.startswith("https://"):
            url = channel
        elif channel.startswith("@"):
            url = f"https://www.youtube.com/{channel}/videos"
        else:
            url = f"https://www.youtube.com/@{channel}/videos"

        try:
            r = subprocess.run(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--playlist-end", str(limit),
                    "--print", "%(id)s\t%(title)s\t%(duration)s\t%(thumbnail)s",
                    "--no-warnings",
                    "--quiet",
                    url,
                ],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            self._error(504, "yt-dlp timed out fetching feed")
            return
        except Exception as e:
            self._error(500, f"Feed fetch failed: {e}")
            return

        if r.returncode != 0:
            err = r.stderr.strip() or "yt-dlp returned non-zero exit code"
            self._error(502, f"Could not fetch channel feed: {err}")
            return

        videos = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 2:
                continue
            vid_id   = parts[0].strip()
            title    = parts[1].strip()
            duration = parts[2].strip() if len(parts) > 2 else ""
            thumb    = parts[3].strip() if len(parts) > 3 else ""
            if not vid_id or vid_id == "NA":
                continue
            videos.append({
                "id":       vid_id,
                "title":    title,
                "duration": duration,
                "thumb":    thumb,
                "url":      f"https://www.youtube.com/watch?v={vid_id}",
            })

        self._json({"channel": url, "videos": videos})

    # ── MJPEG ─────────────────────────────────────────────────────────────────
    def _serve_mjpeg(self, stream: Stream):
        registry.cleanup_done()

        # Start pipeline if not already running
        if stream.status == "starting" and stream._ff_proc is None:
            threading.Thread(target=run_pipeline,
                             args=(stream,), daemon=True).start()

        # Wait up to 20s for first frame
        deadline = time.time() + 20
        while stream.frame is None and stream.status not in ("error", "done"):
            if time.time() > deadline:
                self._error(504, "Timed out waiting for first frame")
                return
            time.sleep(0.1)

        if stream.status == "error":
            detail = f" ({stream.error_detail})" if stream.error_detail else ""
            self._error(502, f"Pipeline error: {stream.error}{detail}")
            return
        if stream.status == "done" and stream.frame is None:
            detail = f" ({stream.error_detail})" if stream.error_detail else ""
            self._error(502, f"Video ended before first frame was produced{detail}")
            return

        self.send_response(200)
        self.send_header("Content-Type",  "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection",    "keep-alive")
        self.send_header("X-Stream-Id",   stream.id)
        self.send_header("X-Stream-Title", self._safe_header_value(stream.title or ""))
        self.end_headers()

        log.info(f"[{stream.id}] Client connected: {self.client_address[0]}")
        frame_interval = 1.0 / MJPEG_FPS
        last_frame = None

        try:
            while True:
                t0 = time.monotonic()

                with stream.lock:
                    frame = stream.frame

                if frame and frame is not last_frame:
                    last_frame = frame
                    boundary = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    )
                    self.wfile.write(boundary + frame + b"\r\n")
                    self.wfile.flush()
                elif stream.status in ("error", "done"):
                    break

                elapsed = time.monotonic() - t0
                time.sleep(max(0.0, frame_interval - elapsed))

        except (BrokenPipeError, ConnectionResetError):
            log.info(f"[{stream.id}] Client disconnected: {self.client_address[0]}")

    @staticmethod
    def _launch_audio_pipeline(url: str, seek_s: float):
        """Spawn yt-dlp | ffmpeg for audio starting at seek_s seconds."""
        yt_cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", "-",
            "--quiet",
            url,
        ]
        ff_cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-ss", f"{seek_s:.3f}",
            "-i", "pipe:0",
            "-vn",
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "mp3",
            "-b:a", "128k",
            "-f", "mp3",
            "pipe:1",
        ]
        yt_proc = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff_proc = subprocess.Popen(ff_cmd, stdin=yt_proc.stdout,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return yt_proc, ff_proc

    def _serve_audio(self, stream: Stream, sync_ms: int = AUDIO_DELAY_MS):
        yt_proc = None
        ff_proc = None
        try:
            # Audio starts from content position 0. The browser delays the
            # /audio request by sync_ms so video gets a head start.
            log.info(f"[{stream.id}] Audio starting")
            yt_proc, ff_proc = self._launch_audio_pipeline(stream.url, seek_s=0.0)

            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            while True:
                chunk = ff_proc.stdout.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            for proc in (ff_proc, yt_proc):
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _html(self, body: str, code: int = 200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, msg: str):
        self._json({"error": msg, "code": code}, code)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Each request handled in its own thread (needed for concurrent MJPEG streams)."""
    daemon_threads = True
    allow_reuse_address = True


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("═" * 52)
    log.info("  Tesla MJPEG Streamer")
    log.info(f"  Listening on http://{HOST}:{PORT}")
    log.info(f"  FPS={MJPEG_FPS}  Quality={FFMPEG_QUALITY}  "
             f"Res={STREAM_WIDTH}×{STREAM_HEIGHT}  MaxStreams={MAX_STREAMS}")
    log.info("═" * 52)

    server = ThreadedHTTPServer((HOST, PORT), Handler)

    def _stop(sig, frame):
        log.info("Shutting down…")
        for s in registry.all_streams():
            s.stop()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)
    server.serve_forever()


if __name__ == "__main__":
    main()
