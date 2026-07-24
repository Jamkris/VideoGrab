# vidgrab

Self-hosted video download relay. Share a page URL from your phone, the home
server downloads the video with [yt-dlp](https://github.com/yt-dlp/yt-dlp),
your phone pulls the file, and the server deletes it immediately. The server
never stores videos permanently.

```
iPhone (share sheet shortcut)
   │  POST /jobs {url}
   ▼
vidgrab (Docker, home server) ── yt-dlp downloads to a temp dir
   │  GET /jobs/{id}         ← shortcut polls until status == "done"
   │  GET /jobs/{id}/file    ← shortcut downloads, server deletes the file
   ▼
Photos app
```

## Storage behavior

- Files live under the mounted data dir (`/usb/video-grab/<job-id>/` on the
  server, `./data/` for local runs) only while a job is in flight.
- After the client downloads the file, it is deleted `DELIVERED_GRACE_SECONDS`
  later (default 10 min) — the grace window lets an interrupted transfer
  retry without re-downloading from the source.
- Files never picked up are deleted after `FILE_TTL_SECONDS` (default 1h).
- The whole data dir is wiped on container restart.
- Budget roughly 1–2 GB of transient space per concurrent 40–50 min 1080p
  video; set `MAX_HEIGHT=720` to roughly halve that.

## Server setup

1. Copy this folder to the server (or paste the compose into Dockge and add
   the other files next to it).
2. `cp .env.example .env` and set `API_KEY` to something long and random
   (`openssl rand -hex 24`).
3. `docker compose up -d --build`
4. Check: `curl http://localhost:8000/health`

Expose `localhost:8000` through your Cloudflare Tunnel as e.g.
`https://dl.example.com` (or just use the VPN address directly). Only the
`vidgrab` service is exposed — the `spoofdpi` sidecar stays internal.

> Note: Cloudflare's 100 MB body limit applies to uploads only; large download
> responses stream through fine. If transfers feel slow or flaky through the
> tunnel, point the shortcut at the VPN address instead — the API is the same.

## API

All endpoints require the `X-API-Key` header.

| Method | Path              | Description                                     |
|--------|-------------------|-------------------------------------------------|
| POST   | `/jobs`           | URL via JSON `{"url"}`, form field, `?url=`, or raw body → `{"id"}` |
| GET    | `/jobs/{id}`      | `{"status", "progress", "title", "error", "count", "files"}` |
| GET    | `/jobs/{id}/file` | The video file; deleted from server after send   |

Status values: `queued → downloading → done → delivered` (or `error` /
`expired`).

Quick test from a laptop:

```bash
curl -s -X POST https://dl.example.com/jobs \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://youtu.be/dQw4w9WgXcQ"}'
# → {"id":"a1b2c3d4e5f6"}
curl -s https://dl.example.com/jobs/a1b2c3d4e5f6 -H "X-API-Key: $KEY"
curl -OJ https://dl.example.com/jobs/a1b2c3d4e5f6/file -H "X-API-Key: $KEY"
```

## iOS Shortcut

Create a new shortcut in the Shortcuts app:

1. Shortcut settings (ⓘ) → enable **Show in Share Sheet**, set input types to
   **URLs** (and optionally Safari web pages / text).
2. **Get Contents of URL**
   - URL: `https://dl.example.com/jobs`
   - Method: `POST`, Request Body: **Form** with field `url` = **Shortcut Input**
     (Form avoids JSON-escaping errors when a shared URL contains stray
     backslashes; the server also strips them defensively.)
   - Headers: `X-API-Key` = your key
3. **Get Dictionary Value** — key `id` (from the previous step). This is the
   job id used below.
4. **Repeat 120 times**
   1. **Wait 15 seconds** (wait first: even short videos need a moment)
   2. **Get Contents of URL** — GET `https://dl.example.com/jobs/<job id>`
      with the `X-API-Key` header
   3. **Get Dictionary Value** — key `status`
   4. **If** status **is** `done`:
      - **Get Dictionary Value** — key `files` (a list, one entry per video)
      - **Repeat with Each** over that list:
        - **Get Dictionary Value** — key `url` from **Repeat Item**
        - **Get Contents of URL** — GET that url with the `X-API-Key` header
          (downloads that video)
        - **Save to Photo Album**
      - **Show Notification** — "Saved <count> ✅"
      - **Stop This Shortcut**

      Iterate `files` with **Repeat Item**, never with `Repeat Index`. This
      block sits inside the polling repeat, and in nested repeats Shortcuts
      hands you two `Repeat Index` variables — picking the outer one indexes
      the file list by the poll count and throws "the index you specified was
      outside of the possible range". Each entry carries its own `url`,
      `filename`, and `index`, so no index math is needed. (`file_urls` /
      `filenames` remain as parallel arrays for existing shortcuts.)
   5. **If** status **is** `error`:
      - **Get Dictionary Value** — key `error` → **Show Notification**
      - **Stop This Shortcut**
5. After the repeat block: **Show Notification** — "Timed out" (only reached
   if 30 minutes pass without completion).

Usage: any app → Share → your shortcut → video lands in Photos.

Tip: with `NTFY_URL` set, the server also pushes a "Ready" notification via
ntfy when the download finishes — handy for very long videos if you'd rather
re-run the shortcut later than keep it polling.

## How it handles awkward sites

Each job escalates through three strategies automatically:

1. **Direct** — yt-dlp's normal extractor (YouTube, X, Instagram, and 1800+
   supported sites, plus generic `<video>`/HLS detection).
2. **Chrome impersonation** — retries with a real browser TLS fingerprint for
   sites that reject non-browser clients (curl_cffi).
3. **Resolver fallback** (`resolver.py`) — for pages that bury the video in a
   nested player iframe and disguise the HLS playlist/segments with innocent
   extensions (`.txt`/`.dat` instead of `.m3u8`/`.ts`). It scrapes the real
   stream URL out of the page and feeds it back to yt-dlp with the right
   Referer. This is what generic download extensions usually can't do.

### ISP SNI blocking (Korea)

If a site loads fine in your browser but the server gets
`Connection reset by peer`, your ISP is likely blocking it by inspecting the
TLS SNI. The bundled **SpoofDPI** sidecar fixes this by fragmenting the TLS
ClientHello — the same idea as Unicorn HTTPS, but applied to the server's
outbound traffic (your phone's Unicorn app can't help a request the server
makes). It's wired up in `docker-compose.yml` via `YTDLP_PROXY`.

## Age-restricted / login-gated content (cookies)

18+ or otherwise login-gated posts (common on X/Twitter) are invisible to
anonymous downloaders — yt-dlp reports "No video could be found." The fix is
to give yt-dlp the cookies of a logged-in session, exactly how public
download sites keep a signed-in account server-side.

1. Log into the site (use a **throwaway/secondary account** — these cookies
   grant full access to whatever account they belong to) in a browser.
2. Export cookies in **Netscape format** with a browser extension such as
   "Get cookies.txt LOCALLY".
3. Save the file as `cookies/cookies.txt` next to `docker-compose.yml` on the
   server, then restart: `docker compose up -d`.
4. Verify: `curl http://localhost:8000/health` should show `"cookies": true`.

Cookies are gitignored and mounted read-only. They expire eventually — re-export
when age-gated downloads start failing again.

## Limitations

- DRM-protected services (Netflix, Tving, etc.) cannot be downloaded — by
  anything, this tool included.
- Sites that require login work only if yt-dlp supports cookie auth for them
  (not wired up here; can be added with a cookies file).
- If a previously working site starts failing, restart the container — it
  self-updates yt-dlp on start, which fixes most extraction breakage.
