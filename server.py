#!/usr/bin/env python3
"""
OpenCarStream MJPEG Streamer
Usage:
  http://yourserver/stream?url=https://youtube.com/watch?v=xxx
  http://yourserver/             → status page
  http://yourserver/health       → health check
"""

import re
import subprocess
import threading
import time
import sys
import os
import signal
import json
import logging
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("streamer")


def _parse_lang_map(raw: str) -> dict[str, str]:
    """
    Parse env mapping in format: "es:ES,en:US" or "en:8.8.8.8".
    Keys are normalized to lowercase language codes.
    """
    result: dict[str, str] = {}
    for part in (raw or "").split(","):
        item = part.strip()
        if not item or ":" not in item:
            continue
        lang, value = item.split(":", 1)
        lang = lang.strip().lower()
        value = value.strip()
        if lang and value:
            result[lang] = value
    return result


# ── Config (override via env vars) ────────────────────────────────────────────
HOST          = os.environ.get("HOST", "0.0.0.0")
PORT          = int(os.environ.get("PORT", "8080"))
MJPEG_FPS     = int(os.environ.get("MJPEG_FPS", "24"))
FFMPEG_QUALITY= int(os.environ.get("FFMPEG_QUALITY", "3"))   # 1=best, 31=worst
STREAM_WIDTH  = int(os.environ.get("STREAM_WIDTH", "1920"))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", "1080"))
MAX_STREAMS   = int(os.environ.get("MAX_STREAMS", "3"))       # concurrent stream slots
AUDIO_DELAY_MS= int(os.environ.get("AUDIO_DELAY_MS", "0"))   # ms to delay video start after audio, to keep streams in sync
LOCAL_MEDIA_VIDEO_DELAY_MS = int(
    os.environ.get("LOCAL_MEDIA_VIDEO_DELAY_MS", "1500")
)
SUBSCRIPTIONS_FILE  = os.environ.get("SUBSCRIPTIONS_FILE", "/config/subscriptions.json")
ACE_STREAMS_FILE    = os.environ.get("ACE_STREAMS_FILE", "/config/ace_streams.json")
# Comma-separated list of Pluto TV language codes to load, e.g. "es,en"
PLUTO_LANGS         = [l.strip() for l in os.environ.get("PLUTO_LANGS", "es,en").split(",") if l.strip()]
PLUTO_REFRESH_SECS  = int(os.environ.get("PLUTO_REFRESH_SECS", str(60 * 60)))  # 1 h
PLUTO_APP_VERSION   = os.environ.get("PLUTO_APP_VERSION", "8.0.0-111b2b9dc00bd0bea9030b30662159ed9e7c8bc6")
# Maps UI language -> Pluto marketing region (country code).
# Default keeps Spanish in ES and routes English to US lineup.
PLUTO_REGION_MAP    = _parse_lang_map(os.environ.get("PLUTO_REGION_MAP", "es:ES,en:US"))
# Optional language -> X-Forwarded-For IP used for boot/channels requests.
# This helps force region-specific lineups when server IP geo differs.
PLUTO_XFF_MAP       = _parse_lang_map(os.environ.get("PLUTO_XFF_MAP", "en:8.8.8.8"))
LOCAL_MEDIA_DIR     = os.environ.get("LOCAL_MEDIA_DIR", "/media/videos")
IPTV_LISTS_DIR      = os.environ.get("IPTV_LISTS_DIR", "/iptv_lists")
LOCAL_MEDIA_EXTS    = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts",
}
IPTV_LIST_EXTS      = {".m3u", ".m3u8"}


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
        self._audio_proc: object | None = None   # separate audio ffmpeg for direct streams
        self.started_at : float | None = None
        self.first_frame_at: float | None = None
        # Audio ring-buffer for direct streams (HLS/MPEG-TS) where a second
        # connection to the source is not viable.
        self._audio_lock   = threading.Lock()
        self._audio_chunks : list[bytes] = []
        self._audio_ready  = threading.Event()
        self._audio_done   = False
        self._frame_history = deque(maxlen=max(MJPEG_FPS * 12, 120))

    def stop(self):
        for proc in [self._ff_proc, self._yt_proc, self._audio_proc]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        self._ff_proc     = None
        self._yt_proc     = None
        self._audio_proc  = None
        with self._audio_lock:
            self._audio_done = True
        self._audio_ready.set()
        with self.lock:
            self._frame_history.clear()

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


# ── Pluto TV channel cache ─────────────────────────────────────────────────────
class PlutoCache:
    def __init__(self):
        self._lock  = threading.Lock()
        self._by_lang: dict[str, list[dict]] = {}
        self._errors:  dict[str, str]        = {}
        # { lang: (device_id, session_token, stitcher_params, refresh_at) }
        self._sessions: dict[str, tuple[str, str, str, float]] = {}

    def get(self, lang: str) -> tuple[list[dict], str]:
        with self._lock:
            return list(self._by_lang.get(lang, [])), self._errors.get(lang, "")

    def get_meta(self, lang: str) -> dict[str, str | int]:
        """Return metadata for a language cache entry."""
        from urllib.parse import parse_qsl
        region, xff = self._lang_context(lang)
        with self._lock:
            sess = self._sessions.get(lang)
        if not sess:
            return {"country": "", "refresh_at": 0, "region": region, "xff": xff}
        _, _token, stitcher_params, refresh_at = sess
        country = ""
        for key, val in parse_qsl(stitcher_params, keep_blank_values=True):
            if key == "country":
                country = val
                break
        return {
            "country": country,
            "refresh_at": int(refresh_at),
            "region": region,
            "xff": xff,
        }

    def langs(self) -> list[str]:
        with self._lock:
            return list(self._by_lang.keys())

    @staticmethod
    def _lang_context(lang: str) -> tuple[str, str]:
        lang_key = (lang or "").strip().lower()
        region = PLUTO_REGION_MAP.get(lang_key, lang_key.upper() or "US")
        xff = PLUTO_XFF_MAP.get(lang_key, "")
        return region, xff

    @staticmethod
    def _apply_stitcher_params(hls_url: str, stitcher_params: str, session_token: str = "") -> str:
        """Merge Pluto stitcher query params into the channel HLS URL."""
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(hls_url)
        # Ensure /v2 prefix on path (Pluto requires /v2/stitch/hls/...)
        path = parts.path
        if path.startswith("/stitch/") and not path.startswith("/v2/"):
            path = "/v2" + path
        merged = dict(parse_qsl(parts.query, keep_blank_values=True))
        for k, v in parse_qsl(stitcher_params, keep_blank_values=True):
            merged[k] = v
        if session_token:
            merged["jwt"] = session_token
        merged["includeExtendedEvents"] = "true"
        merged["masterJWTPassthrough"] = "true"
        query = urlencode(merged, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def build_channel_url(
        self, lang: str, channel_id: str, force_refresh: bool = False
    ) -> tuple[str | None, str]:
        """
        Build a fresh Pluto playback URL for channel_id in lang.
        Returns (url, err). When url is None, err describes the failure.
        """
        if not channel_id:
            return None, "missing channel id"

        if force_refresh:
            self._fetch_lang(lang)

        with self._lock:
            channels = self._by_lang.get(lang, [])
            sess = self._sessions.get(lang)
            channel = next((c for c in channels if c.get("id") == channel_id), None)

        if channel is None:
            return None, f"channel '{channel_id}' not found for lang '{lang}'"
        if not sess:
            return None, f"Pluto TV [{lang}] session unavailable"

        _, session_token, stitcher_params, _ = sess
        hls_url = channel.get("hls_url", "")
        if not hls_url:
            return None, "channel has no HLS URL"
        return self._apply_stitcher_params(hls_url, stitcher_params, session_token), ""

    def _boot(self, lang: str) -> tuple[str, str, str, int] | None:
        """Call Pluto boot API and return (device_id, session_token, stitcher_params, refresh_in_sec)."""
        import urllib.request, uuid
        region, xff = self._lang_context(lang)
        device_id = str(uuid.uuid4())
        url = (
            f"https://boot.pluto.tv/v4/start"
            f"?appName=web&appVersion={PLUTO_APP_VERSION}"
            f"&deviceDNT=0&deviceId={device_id}&deviceMake=chrome"
            f"&deviceModel=web&deviceType=web&deviceVersion=122.0.0"
            f"&clientModelNumber=1.0.0&serverSideAds=false"
            f"&drmCapabilities=widevine%3AL3&blockingMode="
            f"&marketingRegion={region}&clientID={device_id}"
        )
        try:
            headers = {
                "User-Agent": _BROWSER_UA,
                "Accept": "application/json",
                "Referer": "https://pluto.tv/",
                "Origin": "https://pluto.tv",
            }
            if xff:
                headers["X-Forwarded-For"] = xff
            req = urllib.request.Request(
                url, headers=headers
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                data = json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log.warning(f"Pluto TV [{lang}] boot HTTP {e.code}: {body[:500]}")
            return None
        except Exception as e:
            log.warning(f"Pluto TV [{lang}] boot failed: {e}")
            return None
        log.info(f"Pluto TV [{lang}] boot response keys: {list(data.keys())}")
        session_token = data.get("sessionToken", "")
        params = data.get("stitcherParams", "")
        refresh = int(data.get("refreshInSec", 28800))
        if not params:
            log.warning(f"Pluto TV [{lang}] boot returned no stitcherParams. Full response: {raw[:1000]}")
            return None
        return device_id, session_token, params, refresh

    def _fetch_lang(self, lang: str):
        import urllib.request
        boot = self._boot(lang)
        if boot is None:
            with self._lock:
                self._errors[lang] = "boot API failed"
            return
        device_id, session_token, stitcher_params, refresh_in = boot

        _, xff = self._lang_context(lang)
        api_url = (
            f"https://api.pluto.tv/v2/channels"
            f"?lang={lang}&deviceType=web&deviceId={device_id}"
            f"&appName=web&appVersion={PLUTO_APP_VERSION}&clientTime=0"
        )
        try:
            headers = {
                "User-Agent": _BROWSER_UA,
                "Referer": "https://pluto.tv/",
                "Origin": "https://pluto.tv",
            }
            if session_token:
                headers["Authorization"] = f"Bearer {session_token}"
            if xff:
                headers["X-Forwarded-For"] = xff
            req = urllib.request.Request(
                api_url, headers=headers
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
                raw = json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            with self._lock:
                self._errors[lang] = f"HTTP {e.code}"
            log.warning(f"Pluto TV [{lang}] channels HTTP {e.code}: {body[:500]}")
            return
        except Exception as e:
            with self._lock:
                self._errors[lang] = str(e)
            log.warning(f"Pluto TV [{lang}] channels fetch failed: {e}")
            return
        log.info(f"Pluto TV [{lang}] channels response: {len(raw) if isinstance(raw, list) else type(raw).__name__}")

        channels = []
        for ch in raw:
            if not ch.get("isStitched"):
                continue
            urls = ch.get("stitched", {}).get("urls", [])
            hls_url = next(
                (u.get("url", "") for u in urls if u.get("type") == "hls"),
                None,
            )
            if not hls_url:
                continue
            # Keep the original URL template and inject fresh stitcher params.
            stitched_url = self._apply_stitcher_params(hls_url, stitcher_params, session_token)
            channels.append({
                "id":       ch.get("_id", ""),
                "name":     ch.get("name", ""),
                "category": ch.get("category", ""),
                "hls_url":  hls_url,
                "url":      stitched_url,
            })
        channels.sort(key=lambda c: (c["category"], c["name"]))

        with self._lock:
            self._by_lang[lang] = channels
            self._errors.pop(lang, None)
            self._sessions[lang] = (device_id, session_token, stitcher_params,
                                    time.time() + refresh_in)
        meta = self.get_meta(lang)
        log.info(
            f"Pluto TV [{lang}] region={meta.get('region')} country={meta.get('country')} "
            f"xff={meta.get('xff') or '-'}: loaded {len(channels)} channels "
            f"(refresh in {refresh_in//3600}h)"
        )

    def refresh_all(self):
        for lang in PLUTO_LANGS:
            self._fetch_lang(lang)

    def start_background_refresh(self):
        def _loop():
            while True:
                now = time.time()
                for lang in PLUTO_LANGS:
                    with self._lock:
                        _, _, _, refresh_at = self._sessions.get(lang, ("", "", "", 0))
                    if now >= refresh_at:
                        self._fetch_lang(lang)
                time.sleep(300)  # check every 5 min
        threading.Thread(target=_loop, daemon=True).start()


pluto_cache = PlutoCache()


# ── Pipeline ──────────────────────────────────────────────────────────────────
def fetch_title(stream: Stream):
    if _is_direct_stream(stream.url):
        return  # no yt-dlp for direct streams; title stays empty
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


def _is_direct_hls(url: str) -> bool:
    """True for raw HLS manifest URLs that ffmpeg can consume directly."""
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


def _is_local_media_url(url: str) -> bool:
    """True for local file URLs served by the Local Media tab."""
    return url.startswith("file://")


def _is_acestream(url: str) -> bool:
    """True for acestream-http-proxy URLs (MPEG-TS over HTTP)."""
    return "/ace/getstream" in url or "/ace/manifest.m3u8" in url


def _is_pluto_stream(url: str) -> bool:
    """True for Pluto TV stitched stream URLs."""
    from urllib.parse import urlparse
    return "pluto.tv" in (urlparse(url).netloc or "").lower()


def _is_rtp_stream(url: str) -> bool:
    """True for RTP/UDP multicast streams that ffmpeg can consume directly."""
    return url.startswith(("rtp://", "udp://", "rtsp://", "srt://"))


_PRIVATE_IP_RE = re.compile(
    r"^https?://"
    r"(?:127\.\d+\.\d+\.\d+|"
    r"10\.\d+\.\d+\.\d+|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|"
    r"localhost)"
    r"(?::\d+)?/"
)

def _is_local_network_stream(url: str) -> bool:
    """True for HTTP streams served from private/local IPs (e.g. IPTV middleware)."""
    return bool(_PRIVATE_IP_RE.match(url))


def _is_direct_stream(url: str) -> bool:
    """True for any URL ffmpeg can consume directly without yt-dlp."""
    return (_is_direct_hls(url) or _is_acestream(url) or _is_local_media_url(url)
            or _is_rtp_stream(url) or _is_local_network_stream(url))


def _ffmpeg_input_target(url: str) -> str:
    """Return ffmpeg-safe input target from stream url."""
    if _is_local_media_url(url):
        parsed = urlparse(url)
        return unquote(parsed.path or "")
    return url


def _has_supported_media_ext(path: str) -> bool:
    """True when path or its symlink target has an allowed video extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in LOCAL_MEDIA_EXTS:
        return True
    if os.path.islink(path):
        target_ext = os.path.splitext(os.path.realpath(path))[1].lower()
        return target_ext in LOCAL_MEDIA_EXTS
    return False


def _has_supported_iptv_list_ext(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in IPTV_LIST_EXTS


def _parse_extinf_name(line: str) -> str:
    # #EXTINF:-1 ... ,Channel Name
    # Prefer the explicit title after the first comma.
    _, _, tail = line.partition(",")
    title = (tail or "").strip()
    if title:
        return title

    # Fallback to tvg-name metadata if present.
    marker = 'tvg-name="'
    pos = line.find(marker)
    if pos != -1:
        rest = line[pos + len(marker):]
        value, _, _ = rest.partition('"')
        return value.strip()
    return ""


def _parse_iptv_m3u(content: str) -> list[dict[str, str]]:
    streams: list[dict[str, str]] = []
    pending_name = ""

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending_name = _parse_extinf_name(line)
            continue
        if line.startswith("#"):
            continue

        url = line
        name = pending_name or f"Stream {len(streams) + 1}"
        streams.append({"name": name, "url": url})
        pending_name = ""

    return streams


_ace_streams_lock = threading.Lock()

def _load_ace_streams() -> list[dict]:
    if not os.path.isfile(ACE_STREAMS_FILE):
        return []
    try:
        with open(ACE_STREAMS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_ace_streams(streams: list[dict]) -> None:
    os.makedirs(os.path.dirname(ACE_STREAMS_FILE) or ".", exist_ok=True)
    with _ace_streams_lock:
        with open(ACE_STREAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(streams, f, indent=2)


def _scan_iptv_lists() -> tuple[str, list[dict[str, str]], str]:
    base = os.path.abspath(IPTV_LISTS_DIR)
    if not os.path.isdir(base):
        return base, [], f"IPTV lists directory not found: {base}"

    lists: list[dict[str, str]] = []
    try:
        for root, _, names in os.walk(base, followlinks=True):
            for filename in names:
                full = os.path.join(root, filename)
                if not os.path.isfile(full):
                    continue
                if not _has_supported_iptv_list_ext(full):
                    continue
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                name = os.path.splitext(os.path.basename(rel))[0]
                lists.append({"id": rel, "name": name, "path": rel})
    except Exception as e:
        return base, [], f"Failed to scan IPTV lists folder: {e}"

    lists.sort(key=lambda x: x["path"].lower())
    return base, lists, ""


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _direct_input_args(url: str) -> list[str]:
    """ffmpeg input flags for a direct stream URL."""
    from urllib.parse import urlparse, parse_qs
    if _is_local_media_url(url):
        return ["-re"]
    if _is_acestream(url):
        return ["-timeout", "10000000"]
    if _is_rtp_stream(url):
        return ["-rtbufsize", "100M"]
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    headers = ""
    if "pluto.tv" in host:
        country = (parse_qs(parsed.query).get("country", [""])[0] or "").upper()
        xff = ""
        if country:
            for lang_code, region_code in PLUTO_REGION_MAP.items():
                if region_code.upper() == country:
                    xff = PLUTO_XFF_MAP.get(lang_code, "")
                    if xff:
                        break
        headers = (
            "Referer: https://pluto.tv/\r\n"
            "Origin: https://pluto.tv\r\n"
            "Accept-Language: en-US,en;q=0.9\r\n"
        )
        if xff:
            headers += f"X-Forwarded-For: {xff}\r\n"
    args = ["-user_agent", _BROWSER_UA]
    if headers:
        args += ["-headers", headers]
    # Don't use -re for Pluto live HLS: let ffmpeg buffer ahead for smoother output.
    if "pluto.tv" not in host:
        args.append("-re")
    return args


def _start_audio_buffer(stream: Stream):
    """Spawn a dedicated ffmpeg process to fill stream._audio_chunks with MP3."""
    audio_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        *_direct_input_args(stream.url),
        "-i", _ffmpeg_input_target(stream.url),
        "-vn",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "mp3",
        "-b:a", "128k",
        "-f", "mp3",
        "pipe:1",
    ]
    audio_proc = subprocess.Popen(
        audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    with stream.lock:
        stream._audio_proc = audio_proc
    with stream._audio_lock:
        stream._audio_chunks.clear()
        stream._audio_done = False
    stream._audio_ready.clear()

    def _drain():
        try:
            while True:
                chunk = audio_proc.stdout.read(8192)
                if not chunk:
                    break
                with stream._audio_lock:
                    stream._audio_chunks.append(chunk)
                stream._audio_ready.set()
        finally:
            with stream._audio_lock:
                stream._audio_done = True
            stream._audio_ready.set()

    threading.Thread(target=_drain, daemon=True).start()


def _start_muxed_pipeline(stream: Stream):
    """
    Start a single ffmpeg process for direct streams that outputs:
    - video MJPEG frames on stdout (pipe:1)
    - audio MP3 chunks on fd 3 (pipe:3)
    This avoids a second source connection for audio.
    """
    vf = (
        f"scale={STREAM_WIDTH}:{STREAM_HEIGHT}"
        f":force_original_aspect_ratio=decrease,"
        f"pad={STREAM_WIDTH}:{STREAM_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
    )
    audio_r, audio_w = os.pipe()
    audio_out = [
        "-map", "0:a:0?",
        "-vn",
    ]
    # Local files are stable sources; skip async resampler to avoid periodic
    # audible artifacts on some ffmpeg + browser combinations.
    if not _is_local_media_url(stream.url):
        audio_out += ["-af", "aresample=async=1:first_pts=0"]
    audio_out += [
        "-c:a", "mp3",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mp3",
        f"pipe:{audio_w}",
    ]

    ff_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        *_direct_input_args(stream.url),
        "-probesize", "20M",
        "-analyzeduration", "10M",
        "-i", _ffmpeg_input_target(stream.url),
        # Video output (stdout / pipe:1)
        "-map", "0:v:0",
        "-vf", vf,
        "-fps_mode", "cfr",
        "-vcodec", "mjpeg",
        "-q:v", str(FFMPEG_QUALITY),
        "-r", str(MJPEG_FPS),
        "-f", "image2pipe",
        "-vframes", "99999999",
        "pipe:1",
        # Audio output (extra fd / pipe:3)
        *audio_out,
    ]
    try:
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(audio_w,),
        )
    except Exception:
        os.close(audio_r)
        raise
    finally:
        os.close(audio_w)

    with stream._audio_lock:
        stream._audio_chunks.clear()
        stream._audio_done = False
    stream._audio_ready.clear()

    def _drain():
        try:
            with os.fdopen(audio_r, "rb", buffering=0) as audio_pipe:
                while True:
                    chunk = audio_pipe.read(8192)
                    if not chunk:
                        break
                    with stream._audio_lock:
                        stream._audio_chunks.append(chunk)
                    stream._audio_ready.set()
        finally:
            with stream._audio_lock:
                stream._audio_done = True
            stream._audio_ready.set()

    threading.Thread(target=_drain, daemon=True).start()
    return ff_proc


def _run_hls_pipeline(stream: Stream):
    """Pipeline for direct streams (HLS / MPEG-TS / Acestream) — no yt-dlp."""
    is_ace = _is_acestream(stream.url)
    is_pluto = _is_pluto_stream(stream.url)
    is_local = _is_local_media_url(stream.url)
    is_rtp = _is_rtp_stream(stream.url)
    is_lan = _is_local_network_stream(stream.url)
    log.info(
        f"[{stream.id}] Direct pipeline (ace={is_ace}, pluto={is_pluto}, local={is_local}, rtp={is_rtp}, lan={is_lan})"
    )

    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"
    try:
        if is_ace or is_pluto or is_local or is_rtp or is_lan:
            ff_proc = _start_muxed_pipeline(stream)
        else:
            # HLS path still uses a dedicated audio process.
            _start_audio_buffer(stream)
            ff_cmd = [
                "ffmpeg",
                "-loglevel", "error",
                *_direct_input_args(stream.url),
                "-i", _ffmpeg_input_target(stream.url),
                "-vf", (
                    f"scale={STREAM_WIDTH}:{STREAM_HEIGHT}"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={STREAM_WIDTH}:{STREAM_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-fps_mode", "cfr",
                "-vcodec", "mjpeg",
                "-q:v", str(FFMPEG_QUALITY),
                "-r", str(MJPEG_FPS),
                "-an",
                "-f", "image2pipe",
                "-vframes", "99999999",
                "pipe:1",
            ]
            ff_proc = subprocess.Popen(
                ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        with stream.lock:
            stream._ff_proc = ff_proc
            stream.status = "streaming"
            if stream.started_at is None:
                stream.started_at = time.time()

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
                    stream._frame_history.append((time.time(), frame))
                    stream.last_used = time.time()
                    if stream.first_frame_at is None:
                        stream.first_frame_at = time.time()

        ff_rc = ff_proc.poll()
        produced = stream.frame is not None
        with stream.lock:
            if produced:
                stream.status = "done"
            else:
                ff_err = ff_proc.stderr.read(500).decode("utf-8", errors="replace")
                stream.status = "error"
                stream.error = "No video frames from HLS stream"
                stream.error_detail = f"ff_rc={ff_rc} ff_err={ff_err}"
        log.info(f"[{stream.id}] HLS pipeline finished (rc={ff_rc})")
    except Exception as e:
        with stream.lock:
            stream.status = "error"
            stream.error = str(e)
        log.error(f"[{stream.id}] HLS pipeline error: {e}")
    finally:
        stream.stop()


def run_pipeline(stream: Stream):
    log.info(f"[{stream.id}] Starting pipeline for: {stream.url}")
    threading.Thread(target=fetch_title, args=(stream,), daemon=True).start()

    if _is_direct_stream(stream.url):
        _run_hls_pipeline(stream)
        return

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
                        stream._frame_history.append((time.time(), frame))
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
<title>OpenCarStream — Streaming for Tesla vehicles</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@300;500&display=swap');
  :root{--red:#e31937;--dark:#090909;--panel:#111117;--border:#252530;--text:#e0e0ee;--muted:#555568;--input-bg:#0d0d14;--thumb-bg:#1a1a24;}
  @media(prefers-color-scheme:light){:root{--dark:#f4f4f6;--panel:#ffffff;--border:#d8d8e0;--text:#1a1a2e;--muted:#888899;--input-bg:#eaeaf0;--thumb-bg:#dcdce8;}}
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
  .usage{font-family:monospace;font-size:.95rem;background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:14px 18px;line-height:1.9;word-break:break-all;}
  .usage span{color:var(--red);}
  .stream-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);}
  .stream-row:last-child{border-bottom:none;}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;letter-spacing:.06em;font-family:'Orbitron',monospace;}
  .badge.streaming{background:rgba(0,200,100,.15);color:#00a852;border:1px solid rgba(0,200,100,.3);}
  .badge.starting{background:rgba(255,152,0,.12);color:#c97800;border:1px solid rgba(255,152,0,.3);}
  .badge.error{background:rgba(227,25,55,.12);color:var(--red);border:1px solid rgba(227,25,55,.3);}
  .badge.done{background:var(--input-bg);color:var(--muted);border:1px solid var(--border);}
  .empty{color:var(--muted);font-size:.9rem;font-style:italic;}
  a{color:var(--red);text-decoration:none;}a:hover{text-decoration:underline;}
  .env-row{display:flex;gap:24px;flex-wrap:wrap;margin-top:4px;}
  .env-item{font-family:monospace;font-size:.85rem;color:var(--muted);}
  .env-item b{color:var(--text);}
  /* Feed tab */
  .feed-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:18px;}
  .feed-controls input{flex:1;min-width:220px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;font-size:.95rem;}
  .feed-controls button{background:var(--red);color:white;border:0;border-radius:6px;padding:10px 16px;font-family:'Orbitron',monospace;letter-spacing:.08em;cursor:pointer;white-space:nowrap;}
  .feed-status{color:var(--muted);font-size:.9rem;font-style:italic;margin-bottom:10px;min-height:1.4em;}
  .feed-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;}
  .feed-card{background:var(--input-bg);border:1px solid var(--border);border-radius:8px;overflow:hidden;cursor:pointer;transition:border-color .15s;}
  .feed-card:hover{border-color:var(--red);}
  .feed-thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:var(--thumb-bg);display:block;}
  .feed-info{padding:8px 10px;}
  .feed-title{font-size:.85rem;line-height:1.3;color:var(--text);margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .feed-dur{font-family:monospace;font-size:.75rem;color:var(--muted);}
  /* shared input style for start-stream row */
  #yt-id{flex:1;min-width:280px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;}
  select{background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-family:monospace;}
  footer{margin-top:26px;color:var(--muted);font-size:.78rem;letter-spacing:.04em;text-align:center;max-width:760px;line-height:1.5;}
</style>
</head>
<body>
<h1>OPENCARSTREAM</h1>
<p class="sub">A third-party streaming launcher for Tesla’s in-car browser</p>

<div class="tabs">
  <button class="tab-btn active" data-tab="stream">Stream</button>
  <button class="tab-btn" data-tab="feed">YouTube</button>
  <button class="tab-btn" data-tab="twitch">Twitch</button>
  <button class="tab-btn" data-tab="pluto">Pluto TV</button>
  <button class="tab-btn" data-tab="iptv">IPTV</button>
  <button class="tab-btn" data-tab="ace">Acestream</button>
  <button class="tab-btn" data-tab="local">Local Media</button>
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
      <div id="yt-quality-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div id="yt-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
    </div>
    <div style="margin-top:10px;">
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
    <div class="env-item">Pluto TV langs <b>{{pluto_langs}}</b></div>
    <div class="env-item">IPTV lists <b>{{iptv_status}}</b></div>
  </div>
</div>
</div>

<!-- ── Feed tab ── -->
<div class="tab-panel" id="tab-feed">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <div id="feed-quality-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div id="feed-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
    </div>
  </div>

  <!-- Subscriptions panel (shown when cookies available) -->
  <div class="card" id="subs-card" style="display:none;">
    <h2>My subscriptions</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px;">
      <button id="subs-load" style="background:var(--red);color:white;border:0;border-radius:6px;padding:8px 16px;font-family:'Orbitron',monospace;letter-spacing:.08em;cursor:pointer;">LOAD SUBSCRIPTIONS</button>
      <input id="subs-filter" type="text" placeholder="Filter channels…"
             style="flex:1;min-width:180px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;display:none;">
    </div>
    <div class="feed-status" id="subs-status"></div>
    <div id="subs-list" style="display:flex;flex-direction:column;gap:0;"></div>
  </div>

  <!-- YouTube search -->
  <div class="card">
    <h2>Search YouTube</h2>
    <div class="feed-controls">
      <input id="yt-search-input" type="text" placeholder="Search query…">
      <button id="yt-search-go">SEARCH</button>
    </div>
    <div class="feed-status" id="yt-search-status"></div>
    <div class="feed-grid" id="yt-search-grid"></div>
    <div style="text-align:center;margin-top:14px;display:none;" id="yt-search-more-wrap">
      <button id="yt-search-more" style="background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 20px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">LOAD MORE</button>
    </div>
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
    <div style="text-align:center;margin-top:14px;display:none;" id="feed-more-wrap">
      <button id="feed-more" style="background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 20px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">LOAD MORE</button>
    </div>
  </div>
</div>

<!-- ── Twitch tab ── -->
<div class="tab-panel" id="tab-twitch">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <div id="twitch-quality-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div id="twitch-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
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
    <div style="text-align:center;margin-top:14px;display:none;" id="twitch-vod-more-wrap">
      <button id="twitch-vod-more" style="background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 20px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">LOAD MORE</button>
    </div>
  </div>
</div>

<!-- ── Pluto TV tab ── -->
<div class="tab-panel" id="tab-pluto">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <div id="pluto-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <input id="pluto-filter" type="text" placeholder="Filter channels…"
             style="flex:1;min-width:180px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;">
    </div>
  </div>
  <div class="card">
    <h2>Channels</h2>
    <div id="pluto-lang-btns" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;"></div>
    <div class="feed-status" id="pluto-status">Open this tab to load channels.</div>
    <div id="pluto-list"></div>
  </div>
</div>

<!-- ── IPTV tab ── -->
<div class="tab-panel" id="tab-iptv">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <div id="iptv-quality-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div id="iptv-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
    </div>
  </div>
  <div class="card">
    <h2>IPTV lists</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px;">
      <button id="iptv-prev"
              style="background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-family:'Orbitron',monospace;font-size:.9rem;cursor:pointer;">
        ◄
      </button>
      <div id="iptv-list-name"
           style="flex:1;min-width:180px;color:var(--text);font-size:.85rem;text-align:center;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--input-bg);">
        Select a list
      </div>
      <button id="iptv-next"
              style="background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-family:'Orbitron',monospace;font-size:.9rem;cursor:pointer;">
        ►
      </button>
      <button id="iptv-refresh"
              style="background:var(--red);color:white;border:0;border-radius:6px;padding:8px 14px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">
        REFRESH
      </button>
    </div>
    <input id="iptv-filter" type="text" placeholder="Filter streams..."
           style="width:100%;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;margin-bottom:12px;">
    <div class="feed-status" id="iptv-status">Open this tab to load IPTV lists.</div>
    <div id="iptv-streams"></div>
  </div>
</div>

<!-- ── Acestream tab ── -->
<div class="tab-panel" id="tab-ace">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div id="ace-quality-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div id="ace-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
    </div>
  </div>
  <div class="card">
    <h2>Stream by content ID</h2>
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:12px;">
      Enter an Acestream content ID (40-char hex) or a full
      <code style="color:var(--text);">acestream://</code> link.
      Your acestream-http-proxy must be running.
    </p>
    <div class="feed-controls">
      <input id="ace-id" type="text" placeholder="acestream://b08e… or content ID">
      <input id="ace-host" type="text" placeholder="Proxy host:port"
             style="max-width:200px;">
      <button id="ace-go">OPEN STREAM</button>
    </div>
  </div>
  <div class="card">
    <h2>Saved streams</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px;">
      <input id="ace-save-name" type="text" placeholder="Name"
             style="flex:1;min-width:140px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;">
      <input id="ace-save-id" type="text" placeholder="Content ID or acestream:// link"
             style="flex:2;min-width:220px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-family:monospace;">
      <button id="ace-save-btn"
              style="background:var(--red);color:white;border:0;border-radius:6px;padding:8px 14px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">
        SAVE
      </button>
    </div>
    <div id="ace-saved-list"></div>
  </div>
</div>

<!-- ── Info tab ── -->
<div class="tab-panel" id="tab-local">
  <div class="card">
    <h2>Playback options</h2>
    <div style="display:flex;flex-direction:column;gap:10px;">
      <div id="local-sync-btns" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
      <div>
        <button id="local-refresh"
                style="background:var(--red);color:white;border:0;border-radius:6px;padding:8px 14px;font-family:'Orbitron',monospace;font-size:.7rem;letter-spacing:.08em;cursor:pointer;">
          REFRESH LIST
        </button>
      </div>
    </div>
    <p style="font-size:.82rem;color:var(--muted);margin-top:12px;">
      Folder: <code style="color:var(--text);">{{local_media_dir}}</code>
    </p>
  </div>
  <div class="card">
    <h2>Video files</h2>
    <div class="feed-status" id="local-status">Open this tab to load local videos.</div>
    <div id="local-list"></div>
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
      GET /local_media  → JSON local video list<br>
      GET /local_watch<span>?file=</span>relative/path.mp4<span>&amp;sync=1000</span><br>
      GET /iptv_lists  → JSON IPTV playlists from mounted folder<br>
      GET /iptv_streams<span>?list=</span>my-list<br>
      GET /subscriptions  → JSON channel list<br>
      GET /health   → JSON health check<br>
      GET /status   → JSON active streams
    </div>
  </div>
</div>

<footer>Tesla is a trademark of Tesla, Inc. OpenCarStream is unofficial and not affiliated with or endorsed by Tesla. YouTube is a trademark of Google LLC, Twitch is a trademark of Twitch Interactive, Inc., and X/Twitter is a trademark of X Corp.; OpenCarStream is not affiliated with or endorsed by any of them.</footer>

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

  // ── Button group helper ──
  function createButtonGroup(containerId, options, defaultValue) {
    var container = document.getElementById(containerId);
    var state = { value: defaultValue || "" };

    options.forEach(function (opt) {
      var btn = document.createElement("button");
      btn.setAttribute("data-value", opt.value);
      btn.textContent = opt.label;
      btn.style.cssText = "font-family:'Orbitron',monospace;font-size:.7rem;" +
        "letter-spacing:.08em;padding:6px 12px;border-radius:6px;" +
        "border:1px solid var(--border);cursor:pointer;";
      btn.addEventListener("click", function () {
        state.value = opt.value;
        container.querySelectorAll("button").forEach(function (b) {
          b.style.background = b.getAttribute("data-value") === state.value
            ? "var(--red)" : "transparent";
          b.style.color = b.getAttribute("data-value") === state.value
            ? "#fff" : "var(--muted)";
        });
      });
      container.appendChild(btn);
    });

    // Set initial active state
    container.querySelectorAll("button").forEach(function (b) {
      b.style.background = b.getAttribute("data-value") === state.value
        ? "var(--red)" : "transparent";
      b.style.color = b.getAttribute("data-value") === state.value
        ? "#fff" : "var(--muted)";
    });

    return state;
  }

  // ── Stream tab ──
  var idInput    = document.getElementById("yt-id");
  var goButton   = document.getElementById("go-stream");

  var qualitySel = createButtonGroup("yt-quality-btns", [
    { value: "", label: "AUTO" },
    { value: "1080", label: "1080p" },
    { value: "720", label: "720p" },
    { value: "480", label: "480p" },
    { value: "360", label: "360p" },
    { value: "240", label: "240p" },
    { value: "144", label: "144p" }
  ], "");

  var syncSel = createButtonGroup("yt-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

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

  function OpenCarStream() {
    var raw = (idInput.value || "").trim();
    if (!raw) { idInput.focus(); return; }
    window.location.href = buildWatchUrl(
      resolveInputUrl(raw), qualitySel.value, syncSel.value
    );
  }

  goButton.addEventListener("click", OpenCarStream);
  idInput.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) OpenCarStream();
  });

  // ── Twitch tab ──
  var twitchLiveCh   = document.getElementById("twitch-live-channel");
  var twitchLiveGo   = document.getElementById("twitch-live-go");
  var twitchVodCh    = document.getElementById("twitch-vod-channel");
  var twitchVodGo    = document.getElementById("twitch-vod-go");
  var twitchVodSt    = document.getElementById("twitch-vod-status");
  var twitchVodGrid  = document.getElementById("twitch-vod-grid");

  var twitchQuality = createButtonGroup("twitch-quality-btns", [
    { value: "", label: "AUTO" },
    { value: "1080", label: "1080p" },
    { value: "720", label: "720p" },
    { value: "480", label: "480p" },
    { value: "360", label: "360p" },
    { value: "240", label: "240p" }
  ], "");

  var twitchSync = createButtonGroup("twitch-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

  twitchLiveGo.addEventListener("click", function () {
    var ch = (twitchLiveCh.value || "").trim().replace(/^@/, "");
    if (!ch) { twitchLiveCh.focus(); return; }
    var url = "https://www.twitch.tv/" + ch;
    window.location.href = buildWatchUrl(url, twitchQuality.value, twitchSync.value);
  });
  twitchLiveCh.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) twitchLiveGo.click();
  });

  var twitchVodMoreWrap = document.getElementById("twitch-vod-more-wrap");
  var twitchVodMoreBtn  = document.getElementById("twitch-vod-more");
  var twitchVodLimit    = 12;

  function loadTwitchVods(append) {
    var ch = (twitchVodCh.value || "").trim().replace(/^@/, "");
    if (!ch) { twitchVodCh.focus(); return; }
    if (!append) {
      twitchVodLimit = 12;
      twitchVodGrid.innerHTML = "";
      twitchVodMoreWrap.style.display = "none";
    }
    twitchVodSt.textContent = "Loading VODs…";
    var url = "https://www.twitch.tv/" + ch + "/videos";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/feed?channel=" + encodeURIComponent(url) + "&limit=" + twitchVodLimit, true);
    xhr.timeout = 30000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        twitchVodSt.textContent = "Failed to parse response."; return;
      }
      if (data.error) { twitchVodSt.textContent = "Error: " + data.error; return; }
      var videos = data.videos || [];
      if (!videos.length) { twitchVodSt.textContent = "No VODs found."; twitchVodMoreWrap.style.display = "none"; return; }
      if (append) {
        var existing = twitchVodGrid.querySelectorAll(".feed-card").length;
        videos = videos.slice(existing);
      }
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
          window.location.href = buildWatchUrl(v.url, twitchQuality.value, twitchSync.value);
        });
        twitchVodGrid.appendChild(card);
      });
      twitchVodSt.textContent = twitchVodGrid.querySelectorAll(".feed-card").length + " VODs";
      twitchVodMoreWrap.style.display = "block";
    };
    xhr.send();
  }

  twitchVodGo.addEventListener("click", function () { loadTwitchVods(false); });
  twitchVodMoreBtn.addEventListener("click", function () {
    twitchVodLimit += 12;
    loadTwitchVods(true);
  });
  twitchVodCh.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) loadTwitchVods(false);
  });

  // ── Pluto TV tab ──
  var plutoFilter   = document.getElementById("pluto-filter");
  var plutoStatus   = document.getElementById("pluto-status");
  var plutoList     = document.getElementById("pluto-list");
  var plutoLangBtns = document.getElementById("pluto-lang-btns");

  var plutoSync = createButtonGroup("pluto-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

  var plutoByLang   = {};   // { lang: [channels] }
  var plutoMetaByLang = {}; // { lang: { country: "...", refresh_at: n } }
  var plutoActiveLang = null;
  var plutoLangs    = {{pluto_langs_json}};

  function renderPluto(channels) {
    plutoList.innerHTML = "";
    var lastCat = null;
    channels.forEach(function (ch) {
      if (ch.category && ch.category !== lastCat) {
        lastCat = ch.category;
        var hdr = document.createElement("div");
        hdr.style.cssText = "font-family:'Orbitron',monospace;font-size:.7rem;" +
          "letter-spacing:.12em;color:var(--muted);padding:12px 0 4px;" +
          "text-transform:uppercase;border-top:1px solid var(--border);margin-top:4px;";
        hdr.textContent = ch.category;
        plutoList.appendChild(hdr);
      }
      var row = document.createElement("div");
      row.className = "stream-row";
      row.style.cursor = "pointer";
      row.innerHTML =
        '<span style="font-size:.95rem;">' + escHtml(ch.name) + '</span>' +
        '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);">LIVE \u2192</span>';
      row.addEventListener("click", function () {
        if (ch.id && plutoActiveLang) {
          window.location.href =
            "/pluto_watch?lang=" + encodeURIComponent(plutoActiveLang) +
            "&id=" + encodeURIComponent(ch.id) +
            "&sync=" + encodeURIComponent(plutoSync.value);
          return;
        }
        // Backwards fallback if id is missing.
        window.location.href = buildWatchUrl(ch.url, "", plutoSync.value);
      });
      plutoList.appendChild(row);
    });
  }

  function applyPlutoFilter() {
    var all = plutoByLang[plutoActiveLang] || [];
    var meta = plutoMetaByLang[plutoActiveLang] || {};
    var q = (plutoFilter.value || "").toLowerCase().trim();
    var filtered = q
      ? all.filter(function (c) {
          return c.name.toLowerCase().indexOf(q) !== -1 ||
                 c.category.toLowerCase().indexOf(q) !== -1;
        })
      : all;
    renderPluto(filtered);
    var activeTag = plutoActiveLang ? plutoActiveLang.toUpperCase() : "?";
    var countryTag = meta.country ? (" / " + meta.country.toUpperCase()) : "";
    var regionTag = meta.region ? (" req:" + meta.region.toUpperCase()) : "";
    var sameAs = "";
    var activeSig = all.map(function (c) { return c.id || c.name; }).join("|");
    if (activeSig) {
      Object.keys(plutoByLang).forEach(function (otherLang) {
        if (sameAs || otherLang === plutoActiveLang) return;
        var otherSig = (plutoByLang[otherLang] || [])
          .map(function (c) { return c.id || c.name; })
          .join("|");
        if (otherSig && otherSig === activeSig) {
          sameAs = otherLang.toUpperCase();
        }
      });
    }
    var sameNote = sameAs ? (" · same lineup as " + sameAs + " in your region") : "";
    plutoStatus.textContent =
      "[" + activeTag + countryTag + regionTag + "] " +
      filtered.length + (q ? " of " + all.length : "") +
      " channels (no account required)" + sameNote;
  }

  function switchPlutoLang(lang) {
    plutoActiveLang = lang;
    // Update button styles
    plutoLangBtns.querySelectorAll("button").forEach(function (b) {
      b.style.background = b.getAttribute("data-lang") === lang
        ? "var(--red)" : "transparent";
      b.style.color = b.getAttribute("data-lang") === lang
        ? "#fff" : "var(--muted)";
    });
    if (plutoByLang[lang]) {
      applyPlutoFilter();
      return;
    }
    plutoStatus.textContent = "Loading " + lang.toUpperCase() + " channels\u2026";
    plutoList.innerHTML = "";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/pluto_channels?lang=" + encodeURIComponent(lang), true);
    xhr.timeout = 15000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        plutoStatus.textContent = "Failed to load channels."; return;
      }
      if (data.error) { plutoStatus.textContent = "Error: " + data.error; return; }
      plutoByLang[lang] = data.channels || [];
      plutoMetaByLang[lang] = {
        country: data.country || "",
        region: data.region || "",
        xff: data.xff || "",
        refresh_at: data.refresh_at || 0
      };
      if (plutoActiveLang === lang) applyPlutoFilter();
    };
    xhr.send();
  }

  // Build language toggle buttons
  plutoLangs.forEach(function (lang) {
    var btn = document.createElement("button");
    btn.setAttribute("data-lang", lang);
    btn.textContent = lang.toUpperCase();
    btn.style.cssText = "font-family:'Orbitron',monospace;font-size:.7rem;" +
      "letter-spacing:.1em;padding:6px 14px;border-radius:6px;" +
      "border:1px solid var(--border);background:transparent;" +
      "color:var(--muted);cursor:pointer;";
    btn.addEventListener("click", function () { switchPlutoLang(lang); });
    plutoLangBtns.appendChild(btn);
  });

  // Load first language when Pluto tab is first opened
  var plutoOpened = false;
  document.querySelector('[data-tab="pluto"]').addEventListener("click", function () {
    if (plutoOpened) return;
    plutoOpened = true;
    switchPlutoLang(plutoLangs[0]);
  });

  plutoFilter.addEventListener("input", applyPlutoFilter);

  // ── IPTV tab ──
  var iptvPrevBtn   = document.getElementById("iptv-prev");
  var iptvNextBtn   = document.getElementById("iptv-next");
  var iptvListName  = document.getElementById("iptv-list-name");
  var iptvRefreshBtn= document.getElementById("iptv-refresh");
  var iptvFilter    = document.getElementById("iptv-filter");
  var iptvStatus    = document.getElementById("iptv-status");
  var iptvStreamsEl = document.getElementById("iptv-streams");

  var iptvQuality = createButtonGroup("iptv-quality-btns", [
    { value: "", label: "AUTO" },
    { value: "1080", label: "1080p" },
    { value: "720", label: "720p" },
    { value: "480", label: "480p" },
    { value: "360", label: "360p" },
    { value: "240", label: "240p" }
  ], "");

  var iptvSync = createButtonGroup("iptv-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

  var iptvLists = [];
  var iptvStreams = [];
  var iptvCurrentIndex = 0;

  function selectedIptvListId() {
    if (!iptvLists.length || iptvCurrentIndex < 0) return "";
    return iptvLists[iptvCurrentIndex].id || "";
  }

  function updateIptvListDisplay() {
    if (!iptvLists.length) {
      iptvListName.textContent = "No .m3u lists found";
      iptvListName.style.color = "var(--muted)";
      iptvPrevBtn.disabled = true;
      iptvNextBtn.disabled = true;
      iptvPrevBtn.style.opacity = "0.3";
      iptvNextBtn.style.opacity = "0.3";
      return;
    }
    iptvPrevBtn.disabled = false;
    iptvNextBtn.disabled = false;
    iptvPrevBtn.style.opacity = "1";
    iptvNextBtn.style.opacity = "1";
    var current = iptvLists[iptvCurrentIndex];
    iptvListName.textContent = current.name + " (" + (iptvCurrentIndex + 1) + "/" + iptvLists.length + ")";
    iptvListName.style.color = "var(--text)";
  }

  function renderIptvLists() {
    updateIptvListDisplay();
  }

  iptvPrevBtn.addEventListener("click", function () {
    if (!iptvLists.length) return;
    iptvCurrentIndex = (iptvCurrentIndex - 1 + iptvLists.length) % iptvLists.length;
    updateIptvListDisplay();
    loadIptvStreams();
  });

  iptvNextBtn.addEventListener("click", function () {
    if (!iptvLists.length) return;
    iptvCurrentIndex = (iptvCurrentIndex + 1) % iptvLists.length;
    updateIptvListDisplay();
    loadIptvStreams();
  });

  function renderIptvStreams(list) {
    var q = (iptvFilter.value || "").toLowerCase().trim();
    iptvStreamsEl.innerHTML = "";
    var visible = iptvStreams.filter(function (item) {
      if (!q) return true;
      return (item.name || "").toLowerCase().indexOf(q) !== -1;
    });
    if (!visible.length) {
      iptvStreamsEl.innerHTML = '<p class="empty">No streams match this filter.</p>';
      return;
    }
    visible.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "stream-row";
      row.style.cursor = "pointer";
      row.innerHTML =
        '<span style="font-size:.95rem;">' + escHtml(item.name || item.url) + '</span>' +
        '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);">OPEN \u2192</span>';
      row.addEventListener("click", function () {
        window.location.href = buildWatchUrl(item.url, iptvQuality.value, iptvSync.value);
      });
      iptvStreamsEl.appendChild(row);
    });
    iptvStatus.textContent = visible.length + " streams in " + list.name;
  }

  function loadIptvStreams() {
    var listId = selectedIptvListId();
    if (!listId) {
      iptvStatus.textContent = "No IPTV list selected.";
      iptvStreamsEl.innerHTML = "";
      return;
    }
    iptvStatus.textContent = "Loading IPTV streams...";
    iptvStreamsEl.innerHTML = "";
    iptvRefreshBtn.disabled = true;

    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/iptv_streams?list=" + encodeURIComponent(listId), true);
    xhr.timeout = 15000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      iptvRefreshBtn.disabled = false;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        iptvStatus.textContent = "Failed to parse response."; return;
      }
      if (data.error) { iptvStatus.textContent = "Error: " + data.error; return; }
      iptvStreams = data.streams || [];
      if (!iptvStreams.length) {
        iptvStatus.textContent = "No streams found in this list.";
        iptvStreamsEl.innerHTML = '<p class="empty">This list has no playable entries.</p>';
        return;
      }
      iptvFilter.value = "";
      renderIptvStreams(data.list || {name: "selected list"});
    };
    xhr.send();
  }

  function loadIptvLists(autoloadFirst) {
    iptvStatus.textContent = "Scanning IPTV lists folder...";
    iptvStreamsEl.innerHTML = "";
    iptvRefreshBtn.disabled = true;
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/iptv_lists", true);
    xhr.timeout = 10000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      iptvRefreshBtn.disabled = false;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        iptvStatus.textContent = "Failed to parse response."; return;
      }
      if (data.error) { iptvStatus.textContent = "Error: " + data.error; return; }
      iptvLists = data.lists || [];
      renderIptvLists();
      if (!iptvLists.length) {
        iptvStatus.textContent = "No .m3u or .m3u8 files found.";
        return;
      }
      iptvStatus.textContent = iptvLists.length + " IPTV lists found";
      if (autoloadFirst) {
        iptvCurrentIndex = 0;
        updateIptvListDisplay();
        loadIptvStreams();
      }
    };
    xhr.send();
  }

  iptvRefreshBtn.addEventListener("click", function () { loadIptvLists(false); });
  iptvFilter.addEventListener("input", function () {
    if (!iptvStreams.length) return;
    var selected = iptvLists.find(function (item) { return item.id === selectedIptvListId(); }) || {name: "selected list"};
    renderIptvStreams(selected);
  });
  var iptvOpened = false;
  document.querySelector('[data-tab="iptv"]').addEventListener("click", function () {
    if (iptvOpened) return;
    iptvOpened = true;
    loadIptvLists(true);
  });

  // ── Feed tab ──
  var feedChannel  = document.getElementById("feed-channel");
  var feedGoBtn    = document.getElementById("feed-go");
  var feedStatus   = document.getElementById("feed-status");
  var feedGrid     = document.getElementById("feed-grid");
  var feedCardTitle= document.getElementById("feed-card-title");

  var feedQuality = createButtonGroup("feed-quality-btns", [
    { value: "", label: "AUTO" },
    { value: "1080", label: "1080p" },
    { value: "720", label: "720p" },
    { value: "480", label: "480p" },
    { value: "360", label: "360p" },
    { value: "240", label: "240p" },
    { value: "144", label: "144p" }
  ], "");

  var feedSync = createButtonGroup("feed-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

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

  var feedMoreWrap = document.getElementById("feed-more-wrap");
  var feedMoreBtn  = document.getElementById("feed-more");

  // ── YouTube search ──
  var ytSearchInput    = document.getElementById("yt-search-input");
  var ytSearchGoBtn    = document.getElementById("yt-search-go");
  var ytSearchStatus   = document.getElementById("yt-search-status");
  var ytSearchGrid     = document.getElementById("yt-search-grid");
  var ytSearchMoreWrap = document.getElementById("yt-search-more-wrap");
  var ytSearchMoreBtn  = document.getElementById("yt-search-more");
  var ytSearchLimit    = 12;

  function appendSearchCards(videos) {
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
      ytSearchGrid.appendChild(card);
    });
  }

  function runYtSearch(append) {
    var q = (ytSearchInput.value || "").trim();
    if (!q) { ytSearchInput.focus(); return; }
    if (!append) {
      ytSearchLimit = 12;
      ytSearchGrid.innerHTML = "";
      ytSearchMoreWrap.style.display = "none";
    }
    ytSearchStatus.textContent = append ? "Loading more…" : "Searching…";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/ytsearch?q=" + encodeURIComponent(q) + "&limit=" + ytSearchLimit, true);
    xhr.timeout = 30000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status < 200 || xhr.status >= 300) {
        ytSearchStatus.textContent = "Error: " + xhr.status;
        return;
      }
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        ytSearchStatus.textContent = "Failed to parse response.";
        return;
      }
      if (data.error) {
        ytSearchStatus.textContent = "Error: " + data.error;
        return;
      }
      var videos = data.videos || [];
      if (!videos.length) {
        ytSearchStatus.textContent = "No results found.";
        return;
      }
      if (append) {
        var existing = ytSearchGrid.querySelectorAll(".feed-card").length;
        appendSearchCards(videos.slice(existing));
      } else {
        appendSearchCards(videos);
      }
      ytSearchStatus.textContent = ytSearchGrid.querySelectorAll(".feed-card").length + " results";
      ytSearchMoreWrap.style.display = "block";
    };
    xhr.send();
  }

  ytSearchGoBtn.addEventListener("click", function () { runYtSearch(false); });
  ytSearchInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") runYtSearch(false);
  });
  ytSearchMoreBtn.addEventListener("click", function () {
    ytSearchLimit += 12;
    runYtSearch(true);
  });
  var feedLimit    = 12;

  function appendFeedCards(videos) {
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
  }

  function loadFeed(append) {
    var ch = (feedChannel.value || "").trim();
    if (!ch) { feedChannel.focus(); return; }
    if (!append) {
      feedLimit = 12;
      feedGrid.innerHTML = "";
      feedMoreWrap.style.display = "none";
    }
    feedStatus.textContent = "Loading…";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/feed?channel=" + encodeURIComponent(ch) + "&limit=" + feedLimit, true);
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
        feedMoreWrap.style.display = "none";
        return;
      }
      if (append) {
        var existing = feedGrid.querySelectorAll(".feed-card").length;
        appendFeedCards(videos.slice(existing));
      } else {
        appendFeedCards(videos);
      }
      feedStatus.textContent = feedGrid.querySelectorAll(".feed-card").length + " videos";
      feedMoreWrap.style.display = "block";
    };
    xhr.send();
  }

  feedMoreBtn.addEventListener("click", function () {
    feedLimit += 12;
    loadFeed(true);
  });

  function escHtml(s) {
    return (s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  feedGoBtn.addEventListener("click", loadFeed);
  feedChannel.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) loadFeed();
  });

  // ── Acestream tab ──
  var aceIdInput  = document.getElementById("ace-id");
  var aceHost     = document.getElementById("ace-host");
  var aceGo       = document.getElementById("ace-go");
  var aceSaveName = document.getElementById("ace-save-name");
  var aceSaveId   = document.getElementById("ace-save-id");
  var aceSaveBtn  = document.getElementById("ace-save-btn");
  var aceSavedList= document.getElementById("ace-saved-list");

  var aceQuality = createButtonGroup("ace-quality-btns", [
    { value: "", label: "AUTO" },
    { value: "1080", label: "1080p" },
    { value: "720", label: "720p" },
    { value: "480", label: "480p" },
    { value: "360", label: "360p" }
  ], "");

  var aceSync = createButtonGroup("ace-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{audio_delay_ms}}");

  // Persist proxy host in localStorage; streams are stored server-side
  var ACE_HOST_KEY = "ace_proxy_host";

  aceHost.value = localStorage.getItem(ACE_HOST_KEY) || "192.168.1.7:6878";
  aceHost.addEventListener("change", function () {
    localStorage.setItem(ACE_HOST_KEY, aceHost.value.trim());
  });

  function aceContentId(raw) {
    raw = (raw || "").trim();
    // acestream://HASH → extract hash
    if (/^acestream:\/\//i.test(raw)) raw = raw.slice(12);
    // Full URL → extract id param
    if (/^https?:\/\//i.test(raw)) {
      var m = raw.match(/[?&]id=([a-f0-9]{40})/i);
      if (m) return m[1];
    }
    return raw;
  }

  function buildAceUrl(raw) {
    var cid = aceContentId(raw);
    if (!cid) return null;
    var host = (aceHost.value || "").trim() || "192.168.1.7:6878";
    return "http://" + host + "/ace/getstream?id=" + cid;
  }

  function openAceStream() {
    var raw = (aceIdInput.value || "").trim();
    if (!raw) { aceIdInput.focus(); return; }
    var url = buildAceUrl(raw);
    if (!url) { aceIdInput.focus(); return; }
    localStorage.setItem(ACE_HOST_KEY, (aceHost.value || "").trim());
    window.location.href = buildWatchUrl(url, aceQuality.value, aceSync.value);
  }

  aceGo.addEventListener("click", openAceStream);
  aceIdInput.addEventListener("keydown", function (e) {
    if ((e.key || "") === "Enter" || e.keyCode === 13) openAceStream();
  });

  // Saved streams (server-side)
  function renderSaved(list) {
    aceSavedList.innerHTML = "";
    if (!list || !list.length) {
      aceSavedList.innerHTML = '<p class="empty">No saved streams yet.</p>';
      return;
    }
    list.forEach(function (item, idx) {
      var row = document.createElement("div");
      row.className = "stream-row";
      row.innerHTML =
        '<span style="cursor:pointer;flex:1;" data-idx="' + idx + '">' +
        escHtml(item.name) + '</span>' +
        '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);margin-right:12px;">' +
        escHtml(item.id.slice(0, 12)) + '…</span>' +
        '<button data-del="' + idx + '" style="background:transparent;border:1px solid var(--border);' +
        'color:var(--muted);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:.75rem;">✕</button>';
      row.querySelector("[data-idx]").addEventListener("click", function () {
        var url = buildAceUrl(item.id);
        if (url) window.location.href = buildWatchUrl(url, aceQuality.value, aceSync.value);
      });
      row.querySelector("[data-del]").addEventListener("click", function (e) {
        e.stopPropagation();
        var xhr = new XMLHttpRequest();
        xhr.open("DELETE", "/ace_streams?idx=" + idx, true);
        xhr.onload = function () {
          try { renderSaved(JSON.parse(xhr.responseText).streams); } catch(ex) {}
        };
        xhr.send();
      });
      aceSavedList.appendChild(row);
    });
  }

  function fetchSaved() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/ace_streams", true);
    xhr.onload = function () {
      try { renderSaved(JSON.parse(xhr.responseText).streams); } catch(ex) {}
    };
    xhr.send();
  }

  aceSaveBtn.addEventListener("click", function () {
    var name = (aceSaveName.value || "").trim();
    var raw  = (aceSaveId.value  || "").trim();
    if (!name || !raw) return;
    var cid = aceContentId(raw);
    if (!cid) return;
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/ace_streams", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onload = function () {
      try { renderSaved(JSON.parse(xhr.responseText).streams); } catch(ex) {}
    };
    aceSaveName.value = "";
    aceSaveId.value   = "";
    xhr.send(JSON.stringify({name: name, id: cid}));
  });

  fetchSaved();

  // ── Local Media tab ──
  var localRefresh = document.getElementById("local-refresh");
  var localStatus  = document.getElementById("local-status");
  var localList    = document.getElementById("local-list");

  var localSync = createButtonGroup("local-sync-btns", [
    { value: "0", label: "0s" },
    { value: "500", label: "0.5s" },
    { value: "1000", label: "1s" },
    { value: "1500", label: "1.5s" },
    { value: "2000", label: "2s" },
    { value: "2500", label: "2.5s" },
    { value: "3000", label: "3s" },
    { value: "3500", label: "3.5s" },
    { value: "4000", label: "4s" }
  ], "{{local_media_video_delay_ms}}");

  function renderLocalFiles(files) {
    localList.innerHTML = "";
    if (!files.length) {
      localList.innerHTML = '<p class="empty">No video files found in configured folder.</p>';
      return;
    }
    files.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "stream-row";
      row.style.cursor = "pointer";
      row.innerHTML =
        '<span style="font-size:.95rem;">' + escHtml(item.name || item.path) + '</span>' +
        '<span style="font-family:monospace;font-size:.75rem;color:var(--muted);">OPEN \u2192</span>';
      row.addEventListener("click", function () {
        window.location.href =
          "/local_watch?file=" + encodeURIComponent(item.path) +
          "&sync=" + encodeURIComponent(localSync.value);
      });
      localList.appendChild(row);
    });
  }

  function loadLocalFiles() {
    localStatus.textContent = "Loading local files…";
    localList.innerHTML = "";
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/local_media", true);
    xhr.timeout = 10000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) {
        localStatus.textContent = "Failed to parse response."; return;
      }
      if (data.error) { localStatus.textContent = "Error: " + data.error; return; }
      var files = data.files || [];
      localStatus.textContent = files.length + " local videos";
      renderLocalFiles(files);
    };
    xhr.send();
  }

  localRefresh.addEventListener("click", loadLocalFiles);
  var localOpened = false;
  document.querySelector('[data-tab="local"]').addEventListener("click", function () {
    if (localOpened) return;
    localOpened = true;
    loadLocalFiles();
  });
})();
</script>
</body></html>"""

WATCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenCarStream Watch</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@300;500&display=swap');
  :root{--red:#e31937;--dark:#090909;--panel:#111117;--border:#252530;--text:#e0e0ee;}
  @media(prefers-color-scheme:light){:root{--dark:#f4f4f6;--panel:#ffffff;--border:#d8d8e0;--text:#1a1a2e;}}
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
  audio.preload = "auto";
  audio.muted = false;
  audio.volume = 1.0;
  try {
    var p = audio.play();
    if (p && typeof p.catch === "function") p.catch(function () {});
  } catch (e) {}

  // Some browsers (including embedded WebViews) may block autoplay without
  // interaction. Retry on first user gesture.
  var retryPlay = function () {
    try {
      var p2 = audio.play();
      if (p2 && typeof p2.catch === "function") p2.catch(function () {});
    } catch (e) {}
    window.removeEventListener("click", retryPlay, true);
    window.removeEventListener("touchstart", retryPlay, true);
    window.removeEventListener("keydown", retryPlay, true);
  };
  window.addEventListener("click", retryPlay, true);
  window.addEventListener("touchstart", retryPlay, true);
  window.addEventListener("keydown", retryPlay, true);

  // Always request video immediately; server-side frame buffering applies
  // sync_ms without skipping content from the beginning.
  img.src = "/stream" + q;

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
    iptv_status = "mounted" if os.path.isdir(IPTV_LISTS_DIR) else "not mounted"
    return (STATUS_HTML
            .replace("{{stream_count}}", str(len(streams)))
            .replace("{{streams_html}}", streams_html)
            .replace("{{fps}}", str(MJPEG_FPS))
            .replace("{{quality}}", str(FFMPEG_QUALITY))
            .replace("{{width}}", str(STREAM_WIDTH))
            .replace("{{height}}", str(STREAM_HEIGHT))
            .replace("{{max_streams}}", str(MAX_STREAMS))
            .replace("{{audio_delay_ms}}", str(AUDIO_DELAY_MS))
            .replace("{{local_media_video_delay_ms}}", str(LOCAL_MEDIA_VIDEO_DELAY_MS))
            .replace("{{subs_status}}", subs_status)
            .replace("{{iptv_status}}", iptv_status)
            .replace("{{pluto_langs}}", ", ".join(PLUTO_LANGS))
            .replace("{{pluto_langs_json}}", json.dumps(PLUTO_LANGS))
            .replace("{{local_media_dir}}", LOCAL_MEDIA_DIR))

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

    @staticmethod
    def _resolve_local_media_path(rel_path: str | None) -> tuple[str | None, str]:
        if not rel_path:
            return None, "Missing ?file= parameter"
        # Keep path traversal protections (`..`) while allowing symlink targets
        # outside the base directory when they are reachable via entries inside
        # the mounted media tree.
        base = os.path.abspath(LOCAL_MEDIA_DIR)
        target = os.path.abspath(os.path.normpath(os.path.join(base, rel_path)))
        if not (target == base or target.startswith(base + os.sep)):
            return None, "Invalid local media path"
        if not os.path.isfile(target):
            return None, "Local media file not found"
        if not _has_supported_media_ext(target):
            return None, "Unsupported local media extension"
        return target, ""

    @staticmethod
    def _resolve_iptv_list_path(raw_list: str | None) -> tuple[str | None, str]:
        if not raw_list:
            return None, "Missing ?list= parameter"

        requested = raw_list.strip()
        if not requested:
            return None, "Missing ?list= parameter"

        base, lists, err = _scan_iptv_lists()
        if err:
            return None, err

        request_lower = requested.lower()
        for entry in lists:
            if entry["id"].lower() == request_lower:
                return os.path.join(base, entry["id"].replace("/", os.sep)), ""

        # Allow resolving by friendly name (filename without extension).
        by_name = [entry for entry in lists if entry["name"].lower() == request_lower]
        if len(by_name) == 1:
            return os.path.join(base, by_name[0]["id"].replace("/", os.sep)), ""
        if len(by_name) > 1:
            return None, (
                f"Ambiguous IPTV list name '{requested}'. "
                "Use the full list id/path from /iptv_lists."
            )

        return None, f"IPTV list not found: {requested}"

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

        elif path == "/ytsearch":
            q = qs.get("q", [None])[0]
            if not q:
                self._error(400, "Missing ?q= parameter")
                return
            limit = 12
            try:
                raw_limit = qs.get("limit", [None])[0]
                if raw_limit:
                    limit = max(1, min(int(raw_limit), 50))
            except (ValueError, TypeError):
                pass
            self._serve_ytsearch(q.strip(), limit)

        elif path == "/subscriptions":
            self._serve_subscriptions()

        elif path == "/iptv_lists":
            self._serve_iptv_lists()

        elif path == "/iptv_streams":
            list_name = qs.get("list", [None])[0]
            self._serve_iptv_streams(list_name)

        elif path == "/local_media":
            self._serve_local_media()

        elif path == "/local_watch":
            raw_file = qs.get("file", [None])[0]
            raw_sync = qs.get("sync", [None])[0]
            if raw_sync is None or raw_sync == "":
                sync_ms = LOCAL_MEDIA_VIDEO_DELAY_MS
            else:
                try:
                    sync_ms = self._parse_sync_ms(raw_sync)
                except ValueError as e:
                    self._error(400, str(e))
                    return
            local_file, err = self._resolve_local_media_path(raw_file)
            if not local_file:
                self._error(400, err)
                return
            file_url = "file://" + quote(local_file, safe="/")
            registry.cleanup_done()
            stream = registry.get_or_create(
                file_url,
                quality=None,
                reuse_existing=False,
            )
            # Warm local playback so configured sync delay reflects timeline
            # delay rather than ffmpeg startup overhead.
            if stream.status == "starting" and stream._ff_proc is None:
                threading.Thread(target=run_pipeline, args=(stream,), daemon=True).start()
            warm_deadline = time.time() + 8.0
            while (
                stream.frame is None
                and stream.status not in ("error", "done")
                and time.time() < warm_deadline
            ):
                time.sleep(0.05)
            self._html(render_watch_page(stream.id, sync_ms))

        elif path == "/pluto_channels":
            lang = qs.get("lang", [PLUTO_LANGS[0]])[0]
            self._serve_pluto_channels(lang)

        elif path == "/pluto_watch":
            lang = (qs.get("lang", [PLUTO_LANGS[0]])[0] or "").strip().lower()
            channel_id = qs.get("id", [None])[0]
            if not channel_id:
                self._error(400, "Missing ?id= parameter")
                return
            raw_sync = qs.get("sync", [None])[0]
            try:
                sync_ms = self._parse_sync_ms(raw_sync)
            except ValueError as e:
                self._error(400, str(e))
                return
            if lang not in PLUTO_LANGS:
                self._error(400, f"Unsupported Pluto lang '{lang}'")
                return

            # Refresh Pluto session tokens per playback launch to avoid stale
            # signed URLs being rejected with fallback "unsupported device" streams.
            pluto_url, err = pluto_cache.build_channel_url(
                lang, channel_id, force_refresh=True
            )
            if not pluto_url:
                self._error(502, f"Pluto TV stream unavailable: {err}")
                return
            registry.cleanup_done()
            stream = registry.get_or_create(
                pluto_url,
                quality=None,
                reuse_existing=False,
            )
            self._html(render_watch_page(stream.id, sync_ms))

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
            self._serve_mjpeg(stream, sync_ms=sync_ms)

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

        elif path == "/ace_streams":
            self._serve_ace_streams()

        else:
            self._error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/ace_streams":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                item = json.loads(body)
            except Exception:
                self._error(400, "Invalid JSON")
                return
            name = (item.get("name") or "").strip()
            cid  = (item.get("id")   or "").strip()
            if not name or not cid:
                self._error(400, "Missing name or id")
                return
            streams = _load_ace_streams()
            streams.append({"name": name, "id": cid})
            _save_ace_streams(streams)
            self._json({"ok": True, "streams": streams})
        else:
            self._error(404, "Not found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/ace_streams":
            qs  = parse_qs(parsed.query)
            idx_raw = qs.get("idx", [None])[0]
            try:
                idx = int(idx_raw)
            except (TypeError, ValueError):
                self._error(400, "Missing or invalid ?idx= parameter")
                return
            streams = _load_ace_streams()
            if idx < 0 or idx >= len(streams):
                self._error(404, "Index out of range")
                return
            streams.pop(idx)
            _save_ace_streams(streams)
            self._json({"ok": True, "streams": streams})
        else:
            self._error(404, "Not found")

    # ── Ace streams ───────────────────────────────────────────────────────────
    def _serve_ace_streams(self):
        self._json({"streams": _load_ace_streams()})

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

    # ── IPTV lists ────────────────────────────────────────────────────────────
    def _serve_iptv_lists(self):
        base, lists, err = _scan_iptv_lists()
        if err:
            self._error(503, err)
            return
        self._json({"base_dir": base, "lists": lists})

    def _serve_iptv_streams(self, raw_list: str | None):
        target, err = self._resolve_iptv_list_path(raw_list)
        if not target:
            code = 503 if "directory not found" in err.lower() else 400
            self._error(code, err)
            return
        if not _has_supported_iptv_list_ext(target):
            self._error(400, "Unsupported IPTV list extension (use .m3u or .m3u8)")
            return
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            self._error(500, f"Failed to read IPTV list: {e}")
            return

        streams = _parse_iptv_m3u(content)
        self._json({
            "list": {
                "name": os.path.splitext(os.path.basename(target))[0],
                "path": os.path.relpath(target, os.path.abspath(IPTV_LISTS_DIR)).replace(os.sep, "/"),
            },
            "stream_count": len(streams),
            "streams": streams,
        })

    def _serve_local_media(self):
        base = os.path.abspath(LOCAL_MEDIA_DIR)
        if not os.path.isdir(base):
            self._error(503, f"Local media directory not found: {base}")
            return
        files = []
        try:
            for root, _, names in os.walk(base, followlinks=True):
                for name in names:
                    full = os.path.join(root, name)
                    if not os.path.isfile(full):
                        continue
                    if not _has_supported_media_ext(full):
                        continue
                    rel = os.path.relpath(full, base)
                    files.append({
                        "name": name,
                        "path": rel,
                    })
        except Exception as e:
            self._error(500, f"Failed to scan local media folder: {e}")
            return
        files.sort(key=lambda x: x["path"].lower())
        self._json({"base_dir": base, "files": files})

    # ── Pluto TV channels ─────────────────────────────────────────────────────
    def _serve_pluto_channels(self, lang: str):
        channels, err = pluto_cache.get(lang)
        if not channels:
            if err:
                self._error(502, f"Pluto TV [{lang}] unavailable: {err}")
            else:
                self._error(503, "Pluto TV channel list not loaded yet, try again shortly")
            return
        meta = pluto_cache.get_meta(lang)
        self._json({
            "lang": lang,
            "country": meta.get("country", ""),
            "region": meta.get("region", ""),
            "xff": meta.get("xff", ""),
            "refresh_at": meta.get("refresh_at", 0),
            "channels": channels,
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
                    "--print", "%(id)s\t%(title)s\t%(duration)s\t%(thumbnail)s\t%(webpage_url)s",
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
            parts = line.split("\t", 4)
            if len(parts) < 2:
                continue
            vid_id   = parts[0].strip()
            title    = parts[1].strip()
            duration = parts[2].strip() if len(parts) > 2 else ""
            thumb    = parts[3].strip() if len(parts) > 3 else ""
            webpage  = parts[4].strip() if len(parts) > 4 else ""
            if not vid_id or vid_id == "NA":
                continue
            # Use the canonical webpage URL when available; fall back to
            # building a YouTube URL from the ID for backwards compatibility.
            if webpage and webpage != "NA":
                video_url = webpage
            else:
                video_url = f"https://www.youtube.com/watch?v={vid_id}"
            # yt-dlp returns NA for thumbnails in flat-playlist mode on YouTube.
            # The thumbnail URL is deterministic from the video ID.
            if (not thumb or thumb == "NA") and "youtube.com" in video_url:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
            videos.append({
                "id":       vid_id,
                "title":    title,
                "duration": duration,
                "thumb":    thumb,
                "url":      video_url,
            })

        self._json({"channel": url, "videos": videos})

    def _serve_ytsearch(self, query: str, limit: int = 12):
        search_url = f"ytsearch{limit}:{query}"
        try:
            r = subprocess.run(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--print", "%(id)s\t%(title)s\t%(duration)s\t%(thumbnail)s\t%(webpage_url)s",
                    "--no-warnings",
                    "--quiet",
                    search_url,
                ],
                capture_output=True, text=True, timeout=25,
            )
        except subprocess.TimeoutExpired:
            self._error(504, "yt-dlp timed out during search")
            return
        except Exception as e:
            self._error(500, f"Search failed: {e}")
            return

        if r.returncode != 0:
            err = r.stderr.strip() or "yt-dlp returned non-zero exit code"
            self._error(502, f"Search failed: {err}")
            return

        videos = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 4)
            if len(parts) < 2:
                continue
            vid_id   = parts[0].strip()
            title    = parts[1].strip()
            duration = parts[2].strip() if len(parts) > 2 else ""
            thumb    = parts[3].strip() if len(parts) > 3 else ""
            webpage  = parts[4].strip() if len(parts) > 4 else ""
            if not vid_id or vid_id == "NA":
                continue
            if webpage and webpage != "NA":
                video_url = webpage
            else:
                video_url = f"https://www.youtube.com/watch?v={vid_id}"
            if (not thumb or thumb == "NA"):
                thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
            videos.append({
                "id":       vid_id,
                "title":    title,
                "duration": duration,
                "thumb":    thumb,
                "url":      video_url,
            })

        self._json({"videos": videos})

    # ── MJPEG ─────────────────────────────────────────────────────────────────
    def _serve_mjpeg(self, stream: Stream, sync_ms: int = 0):
        registry.cleanup_done()
        delay_s = max(0.0, sync_ms / 1000.0)

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
        if delay_s > 0:
            # Let delayed playback have enough buffered frames so the first
            # shown frame starts near content time 0.
            while stream.status not in ("error", "done"):
                with stream.lock:
                    if stream._frame_history:
                        oldest_ts = stream._frame_history[0][0]
                        newest_ts = stream._frame_history[-1][0]
                        if newest_ts - oldest_ts >= delay_s:
                            break
                if time.time() > deadline:
                    break
                time.sleep(0.05)

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
                    if delay_s <= 0:
                        frame = stream.frame
                    else:
                        cutoff = time.time() - delay_s
                        frame = None
                        for ts, candidate in reversed(stream._frame_history):
                            if ts <= cutoff:
                                frame = candidate
                                break

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
        log.info(f"[{stream.id}] Audio starting")

        if _is_direct_stream(stream.url):
            # Audio may be requested before /stream starts the pipeline.
            if stream.status == "starting" and stream._ff_proc is None:
                threading.Thread(target=run_pipeline,
                                 args=(stream,), daemon=True).start()
            # For direct streams the audio is already being captured into
            # stream._audio_chunks by _start_audio_buffer. Drain from there
            # instead of opening a second connection to the source.
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                cursor = 0
                sent_bytes = 0
                while True:
                    # Wait for new chunks to appear
                    stream._audio_ready.wait(timeout=5)
                    stream._audio_ready.clear()
                    with stream._audio_lock:
                        new_chunks = stream._audio_chunks[cursor:]
                        cursor += len(new_chunks)
                        done = stream._audio_done
                    for ch in new_chunks:
                        self.wfile.write(ch)
                        sent_bytes += len(ch)
                    self.wfile.flush()
                    if done and not new_chunks:
                        break
                log.info(f"[{stream.id}] Direct audio ended (bytes={sent_bytes})")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        yt_proc = None
        ff_proc = None
        try:
            # Audio starts from content position 0. The browser delays the
            # /audio request by sync_ms so video gets a head start.
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
    log.info("  OpenCarStream MJPEG Streamer")
    log.info(f"  Listening on http://{HOST}:{PORT}")
    log.info(f"  FPS={MJPEG_FPS}  Quality={FFMPEG_QUALITY}  "
             f"Res={STREAM_WIDTH}×{STREAM_HEIGHT}  MaxStreams={MAX_STREAMS}")
    log.info("═" * 52)

    pluto_cache.start_background_refresh()

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
