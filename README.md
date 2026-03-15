# OpenCarStream - MJPEG Streamer for the integrated browser in your car

Stream any video to your Tesla browser via MJPEG.  
No `<video>` element inside.
Supports YouTube, Twitch, X/Twitter, Pluto TV, and more.

---

## Architecture

```
Tesla browser
    │  GET /watch?url=https://youtube.com/watch?v=xxx
    ▼
[nginx] (optional TLS + DDNS domain)
    │
    ▼
[streamer container]
  yt-dlp stdout
    │ pipe
  ffmpeg stdin → JPEG frames → multipart/x-mixed-replace
    │
    └──► Tesla sees a fast-updating <img> — not a <video>
```

---

## Quick start

### 1. Clone / copy this folder to your server

```bash
scp -r opencarstream/ user@yourserver:~/
ssh user@yourserver
cd opencarstream
```

### 2. Build and start

```bash
docker compose up -d --build
```

If Docker Hub is flaky in your region, override the Python base image source:

```bash
PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim docker compose up -d --build
```

### 3. Test locally

```
http://localhost:33333/health
http://localhost:33333/
```

### 4. Open in car browser

Navigate to your server's status page and use the **Stream** tab, or go directly to:

```
http://YOUR_SERVER_IP:33333/
```

or directly using the url

```
http://YOUR_SERVER_IP:33333/watch?url=https://www.youtube.com/watch?v=VIDEO_ID
```

---

## Channel feed & subscriptions

The **Channel Feed** tab lets you browse recent uploads from any YouTube channel
and click to stream them directly.

### Browsing a single channel

Enter `@channelhandle` or a full channel URL in the feed tab and click **Load Feed**.

### Syncing your YouTube subscriptions

Run `sync_subscriptions.py` **once on your home machine** to generate a
`subscriptions.json` file. The streamer reads this static file — no cookies
or YouTube access needed inside the container.

#### Step 1 — generate subscriptions.json

Make sure you are logged in to YouTube in your browser, then run:

```bash
# Chrome
uv run sync_subscriptions.py --browser chrome

# Firefox
uv run sync_subscriptions.py --browser firefox

# Other supported browsers: chromium, brave, edge, opera, safari
uv run sync_subscriptions.py --browser brave
```

yt-dlp reads your browser's cookie store directly — no export or extension
needed. The script fetches only the channel list (not videos) and finishes
in a few seconds. You will see output like:

```
Fetching subscriptions from YouTube…
✔ 112 channels written to subscriptions.json
```

Re-run this command whenever you follow or unfollow channels.

#### Step 2 — mount it in the container

`docker-compose.yml` already has the volume configured:

```yaml
volumes:
  - ./subscriptions.json:/subscriptions.json:ro
```

After generating the file, restart the container to pick it up:

```bash
docker compose restart streamer
```

#### Step 3 — use it in the UI

Open the **Channel Feed** tab. A **My Subscriptions** panel will appear
automatically. Click **Load Subscriptions**, then click any channel to browse
its recent uploads and stream a video.

> **Note:** if you prefer to export cookies manually instead of using
> `--browser`, install the
> [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
> Chrome extension, export while on youtube.com, then run:
> `uv run sync_subscriptions.py --cookies /path/to/cookies.txt`

---

## Expose to the internet (DDNS)

### Option A — No-IP / DuckDNS / Dynu (port forwarding)

1. Sign up for a free DDNS provider (no-ip.com, duckdns.org, dynu.com)
2. Install their update client on your server or router
3. Port-forward TCP 33333 (or 443 if using nginx TLS) on your router to the server
4. Open Tesla browser to: `http://yourname.ddns.net:33333/`

### Option B — Cloudflare Tunnel (no port forwarding needed)

```bash
# Install cloudflared
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# Authenticate and create tunnel
cloudflared tunnel login
cloudflared tunnel create opencarstream
cloudflared tunnel route dns opencarstream stream.yourdomain.com

# Run (or add to systemd)
cloudflared tunnel run --url http://localhost:33333 opencarstream
```

Tesla URL becomes: `https://stream.yourdomain.com/`

### Option C — Nginx + Let's Encrypt TLS (with DDNS domain)

1. Uncomment the `nginx` service in `docker-compose.yml`
2. Edit `nginx.conf` → set your domain in `server_name`
3. Get a free TLS cert:

```bash
sudo apt install certbot
sudo certbot certonly --standalone -d stream.yourdomain.com
sudo cp /etc/letsencrypt/live/stream.yourdomain.com/fullchain.pem certs/cert.pem
sudo cp /etc/letsencrypt/live/stream.yourdomain.com/privkey.pem   certs/key.pem
```

4. `docker compose up -d`

---

## Environment variables

| Variable              | Default               | Description                             |
|-----------------------|-----------------------|-----------------------------------------|
| `PORT`                | 8080                  | HTTP port inside container              |
| `MJPEG_FPS`           | 24                    | Frames/sec sent to client               |
| `FFMPEG_QUALITY`      | 3                     | JPEG quality: 1=best, 31=smallest       |
| `STREAM_WIDTH`        | 1920                  | Output width (px)                       |
| `STREAM_HEIGHT`       | 1080                  | Output height (px)                      |
| `MAX_STREAMS`         | 3                     | Max parallel video streams              |
| `AUDIO_DELAY_MS`      | 0                     | ms to delay video start after audio     |
| `LOCAL_MEDIA_DIR`     | /media/videos         | Local Media folder path inside container |
| `SUBSCRIPTIONS_FILE`  | /subscriptions.json   | Path to subscriptions JSON inside container |

Override in `docker-compose.yml` under `environment:`.

---

## Local Media in Docker

The **Local Media** tab reads files from inside the container, not directly from
your host filesystem. Use a bind mount:

```yaml
environment:
  LOCAL_MEDIA_DIR: /media/videos
volumes:
  - ./local-media:/media/videos:ro
```

Then put your videos on the host in `./local-media` (next to `docker-compose.yml`),
or change the left side to any host path, for example:

```yaml
volumes:
  - /home/username/oocal-media:/media/videos:ro
```

After changing mounts/env:

```bash
docker compose up -d
```

---

## Supported sources

Any URL that yt-dlp supports works in the Stream tab or `/watch?url=`:

| Source        | Example URL |
|---------------|-------------|
| YouTube       | `https://www.youtube.com/watch?v=VIDEO_ID` |
| Twitch live   | `https://www.twitch.tv/channelname` |
| Twitch VOD    | `https://www.twitch.tv/videos/VOD_ID` |
| X / Twitter   | `https://x.com/user/status/TWEET_ID` |
| Pluto TV      | Built-in channel list in the Pluto TV tab (US, no account) |

For Twitch and YouTube VODs, use the **Twitch** and **YouTube** tabs respectively
to browse and pick a stream. For X/Twitter, paste the tweet URL directly in the
Stream tab.

---

## API endpoints

| Endpoint                        | Description                                      |
|---------------------------------|--------------------------------------------------|
| `GET /`                         | Status dashboard (Stream / YouTube / Twitch / Pluto TV / Info tabs) |
| `GET /watch?url=…`              | Watch page (MJPEG video + audio)                 |
| `GET /feed?channel=URL&limit=N` | JSON list of recent uploads for a channel/user   |
| `GET /subscriptions`            | JSON channel list from subscriptions.json        |
| `GET /health`                   | `{"ok":true}` — for uptime monitors              |
| `GET /status`                   | JSON list of active streams                      |

---

## Legal Notice

OpenCarStream is a third-party, unofficial project.
Tesla is a trademark of Tesla, Inc. OpenCarStream is unofficial and not affiliated with or endorsed by Tesla.
YouTube is a trademark of Google LLC, Twitch is a trademark of Twitch Interactive, Inc., and X/Twitter is a trademark of X Corp.; OpenCarStream is not affiliated with or endorsed by any of them.

---

## Tips

- **Bookmark it in Tesla**: Save `http://yourserver/` as a bookmark and use the UI
- **Lower bandwidth**: Set `MJPEG_FPS=15` and `FFMPEG_QUALITY=6` in docker-compose.yml
- **Update yt-dlp** (YouTube changes frequently):
  ```bash
  docker compose build --no-cache && docker compose up -d
  ```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Black screen in Tesla | Make sure URL ends in `/watch?url=...` not just `/` |
| `502 Pipeline error` | yt-dlp may need updating: rebuild image |
| `DeadlineExceeded` while pulling base image | Retry with a mirror: `PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim docker compose build --no-cache` |
| Stream stutters on LAN | Lower `MJPEG_FPS` to 12–15 |
| Can't reach from internet | Check port forwarding / firewall; try `curl http://yourserver:33333/health` from outside |
| Container exits immediately | `docker compose logs streamer` to see the error |
| Subscriptions panel not visible | `subscriptions.json` not mounted — check volume in docker-compose.yml |
| `‼ yt-dlp failed` in sync script | Make sure you are logged in to YouTube in the browser you specified |
