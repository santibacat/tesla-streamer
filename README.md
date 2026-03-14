# Tesla MJPEG Streamer 🚗

Stream any YouTube video to your Tesla browser via MJPEG.  
No `<video>` element → Tesla's driving speed-lock doesn't apply.

---

## Architecture

```
Tesla browser
    │  GET /stream?url=https://youtube.com/watch?v=xxx
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
scp -r tesla-streamer/ user@yourserver:~/
ssh user@yourserver
cd tesla-streamer
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
http://localhost:8080/health
http://localhost:8080/stream?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

### 4. Open in Tesla browser

```
http://YOUR_SERVER_IP:8080/stream?url=https://www.youtube.com/watch?v=VIDEO_ID
```

---

## Expose to the internet (DDNS)

### Option A — No-IP / DuckDNS / Dynu (port forwarding)

1. Sign up for a free DDNS provider (no-ip.com, duckdns.org, dynu.com)
2. Install their update client on your server or router
3. Port-forward TCP 8080 (or 443 if using nginx TLS) on your router to the server
4. Open Tesla browser to: `http://yourname.ddns.net:8080/stream?url=...`

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
cloudflared tunnel create tesla-stream
cloudflared tunnel route dns tesla-stream stream.yourdomain.com

# Run (or add to systemd)
cloudflared tunnel run --url http://localhost:8080 tesla-stream
```

Tesla URL becomes: `https://stream.yourdomain.com/stream?url=...`

### Option C — Nginx + Let's Encrypt TLS (with DDNS domain)

1. Uncomment the `nginx` service in `docker-compose.yml`
2. Edit `nginx.conf` → set your domain in `server_name`
3. Get a free TLS cert:

```bash
# On the host (not inside docker)
sudo apt install certbot
sudo certbot certonly --standalone -d stream.yourdomain.com
sudo cp /etc/letsencrypt/live/stream.yourdomain.com/fullchain.pem certs/cert.pem
sudo cp /etc/letsencrypt/live/stream.yourdomain.com/privkey.pem   certs/key.pem
```

4. `docker compose up -d`

---

## Environment variables

| Variable         | Default | Description                        |
|------------------|---------|------------------------------------|
| `PORT`           | 8080    | HTTP port inside container         |
| `MJPEG_FPS`      | 24      | Frames/sec sent to client          |
| `FFMPEG_QUALITY` | 3       | JPEG quality: 1=best, 31=smallest  |
| `STREAM_WIDTH`   | 1920    | Output width (px)                  |
| `STREAM_HEIGHT`  | 1080    | Output height (px)                 |
| `MAX_STREAMS`    | 3       | Max parallel video streams         |

Override in `docker-compose.yml` under `environment:`.

---

## API endpoints

| Endpoint             | Description                                   |
|----------------------|-----------------------------------------------|
| `GET /`              | Status dashboard (HTML)                       |
| `GET /health`        | `{"ok":true}` — for uptime monitors           |
| `GET /status`        | JSON list of active streams                   |
| `GET /stream?url=…`  | MJPEG stream — open directly in Tesla browser |

---

## Tips

- **Bookmark it in Tesla**: Save `http://yourserver/stream?url=YOUR_FAVOURITE_URL` as a bookmark
- **Lower bandwidth**: Set `MJPEG_FPS=15` and `FFMPEG_QUALITY=6` in docker-compose.yml
- **Age-restricted videos**: Mount a `cookies.txt` (exported from your browser with yt-dlp cookie extension), uncomment the volume in docker-compose.yml, and add `--cookies /app/cookies.txt` to the yt-dlp command in server.py
- **Update yt-dlp** (YouTube changes frequently):
  ```bash
  docker compose build --no-cache && docker compose up -d
  ```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Black screen in Tesla | Make sure URL ends in `/stream?url=...` not just `/` |
| `502 Pipeline error` | yt-dlp may need updating: rebuild image |
| `DeadlineExceeded` / `i/o timeout` while pulling `python:3.12-slim` | Retry with a reachable mirror: `PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim docker compose build --no-cache` |
| Stream stutters on LAN | Lower `MJPEG_FPS` to 12–15 |
| Can't reach from internet | Check port forwarding / firewall; try `curl http://yourserver:8080/health` from outside |
| Container exits immediately | `docker compose logs streamer` to see the error |
