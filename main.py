"""vidgrab — self-hosted video download relay.

Accepts a page URL, downloads its video with yt-dlp on the server,
serves the file to the client exactly once, then deletes it.
Nothing is kept on disk beyond FILE_TTL_SECONDS as a safety net.

Endpoints (all require X-API-Key header):
    POST /jobs               url via JSON body, form field, ?url=, or raw body
                             -> {"id": "..."}
    GET  /jobs/{id}          -> {"status": "...", "progress": 42.0, "count": N,
                                 "files": [{"index", "url", "filename"}], ...}
    GET  /jobs/{id}/file     -> the primary (largest) video file
    GET  /jobs/{id}/file/{i} -> the i-th video (0-based) for multi-video posts
    Files are swept from the server a short grace period after delivery.
"""

import asyncio
import json
import os
import re
import shutil
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from resolver import BROWSER_UA, resolve_stream

API_KEY = os.environ.get("API_KEY", "")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/data"))
FILE_TTL_SECONDS = int(os.environ.get("FILE_TTL_SECONDS", "3600"))
# Keep the file briefly after a successful transfer so a client whose
# download died mid-way (flaky cellular) can retry without re-downloading.
DELIVERED_GRACE_SECONDS = int(os.environ.get("DELIVERED_GRACE_SECONDS", "600"))
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
NTFY_URL = os.environ.get("NTFY_URL", "")  # optional, e.g. https://ntfy.example.com/vidgrab
# Optional proxy for outbound downloads (http://, https://, socks5://).
# Useful when the local ISP resets connections to certain sites (SNI blocking).
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
# Optional Netscape-format cookies.txt exported from a logged-in browser.
# Required for age-restricted / login-gated content (e.g. 18+ tweets), which
# is invisible to anonymous guests. Use a throwaway account — cookies grant
# full access to whatever account they came from.
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")

# Prefer H.264 + AAC so the file imports cleanly into the iOS Photos app
# (VP9/AV1 streams save fine but Photos may refuse or fail to play them).
FORMAT = (
    f"bv*[height<={MAX_HEIGHT}][vcodec^=avc1]+ba[acodec^=mp4a]"
    f"/b[ext=mp4][height<={MAX_HEIGHT}]"
    f"/bv*[height<={MAX_HEIGHT}]+ba/b"
)

jobs: dict[str, dict] = {}
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# Map noisy yt-dlp / network errors to short, user-facing messages (shown in the
# iOS Shortcut). Matching is case-insensitive and order matters: the first
# needle found in the raw error wins, so list more specific phrases first.
# Anything unmatched falls through to the raw yt-dlp text so failures stay
# debuggable on the client too.
FRIENDLY_ERRORS: tuple[tuple[str, str], ...] = (
    ("protected tweet", "비공개(잠긴) 계정의 트윗이라 받을 수 없어요."),
    ("not authorized to view", "비공개(잠긴) 계정의 트윗이라 받을 수 없어요."),
    ("no video could be found", "이 게시물에는 영상이 없어요 (이미지·텍스트만 있는 글일 수 있어요)."),
    ("unsupported url", "지원하지 않는 페이지예요 (영상 링크를 찾지 못했어요)."),
    ("confirm your age", "로그인이 필요한 영상이에요 (로그인/연령 제한)."),
    ("sign in to confirm", "로그인이 필요한 영상이에요 (로그인 확인이 필요해요)."),
    ("private video", "비공개 영상이에요."),
    ("http error 403", "서버가 접근을 차단했어요 (403)."),
    ("forbidden", "서버가 접근을 차단했어요 (403)."),
    ("has been removed", "삭제된 영상이에요."),
    ("no longer available", "영상을 더 이상 볼 수 없어요."),
    ("video unavailable", "영상을 더 이상 볼 수 없어요."),
)


def friendly_error(raw: str) -> str:
    """Turn a raw yt-dlp/network error into a short message for the client.

    Unknown errors pass through (truncated) so unmapped failures remain
    diagnosable from the client without a server log round-trip.
    """
    lowered = raw.lower()
    for needle, message in FRIENDLY_ERRORS:
        if needle in lowered:
            return message
    return raw[:500]


def require_key(x_api_key: str = Header(default="")) -> None:
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


def notify(message: str) -> None:
    if not NTFY_URL:
        return
    try:
        req = urllib.request.Request(NTFY_URL, data=message.encode(), method="POST")
        urllib.request.urlopen(req, timeout=10)
    except OSError:
        pass  # notification is best-effort


def run_ytdlp(url: str, opts: dict) -> dict:
    """Download `url` with yt-dlp, escalating through several strategies.

    The proxy is applied only as a fallback, not by default: most sites
    (YouTube, X/Twitter) work — and authenticate — better on a direct
    connection, and routing them through the DPI-bypass proxy can break login.
    Only ISP-blocked sites need it, so we try direct first and reach for the
    proxy afterwards.

    Order: direct → direct+Chrome-impersonation → proxied → proxied+impersonation
    → resolver fallback (scrape an HLS stream out of a nested player iframe).
    """
    from yt_dlp.networking.impersonate import ImpersonateTarget

    chrome = ImpersonateTarget("chrome")
    variants = [{}, {"impersonate": chrome}]
    if YTDLP_PROXY:
        variants += [{"proxy": YTDLP_PROXY}, {"proxy": YTDLP_PROXY, "impersonate": chrome}]

    first_err: Exception | None = None
    for extra in variants:
        try:
            with yt_dlp.YoutubeDL({**opts, **extra}) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as err:
            if first_err is None:
                first_err = err

    # Last resort: the page hides its stream behind a player iframe with
    # disguised extensions. Scrape it (through the proxy, since such sites are
    # usually the blocked ones) and hand the real URL back to yt-dlp.
    stream = resolve_stream(url, proxy=YTDLP_PROXY)
    if stream is None:
        raise first_err
    hls_opts = {
        **opts,
        "proxy": YTDLP_PROXY or None,
        "hls_prefer_native": True,
        # Some CDNs gate the stream on a specific Referer (not the page URL);
        # the resolver reports which one made the playlist verify.
        "http_headers": {"Referer": stream.referer or url, "User-Agent": BROWSER_UA},
    }
    with yt_dlp.YoutubeDL(hls_opts) as ydl:
        info = ydl.extract_info(stream.url, download=True)
    if stream.title:  # the m3u8 filename ("master") is a poor title
        info["title"] = stream.title
    return info


def download_worker(job_id: str) -> None:
    job = jobs[job_id]
    job["status"] = "downloading"
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job["progress"] = round(d.get("downloaded_bytes", 0) / total * 100, 1)

    opts = {
        "outtmpl": str(out_dir / "%(title).80B [%(id).40B].%(ext)s"),
        "format": FORMAT,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }
    # yt-dlp writes refreshed cookies back to the file, but the mounted copy is
    # read-only. Work from a per-job writable copy so it can update tokens
    # (which also helps auth) without touching the source of truth.
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        work_cookies = out_dir / "cookies.txt"
        shutil.copyfile(COOKIES_FILE, work_cookies)
        opts["cookiefile"] = str(work_cookies)
    try:
        info = run_ytdlp(job["url"], opts)
        media = [p for p in out_dir.iterdir() if p.name != "cookies.txt"]
        # A single post can hold several videos; keep them all, largest first.
        files = sorted(media, key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            raise RuntimeError("download produced no file")
        job["files"] = [str(p) for p in files]
        job["filepath"] = str(files[0])  # primary; kept for the un-indexed /file route
        job["title"] = (info or {}).get("title", "video")
        job["progress"] = 100.0
        job["status"] = "done"
        notify(f"Ready: {job['title']}")
    except Exception as exc:  # yt-dlp raises many exception types
        raw = str(exc)
        job["status"] = "error"
        job["error"] = friendly_error(raw)
        shutil.rmtree(out_dir, ignore_errors=True)
        # Admin notification keeps the raw error for debugging; the client sees
        # the friendly message via GET /jobs/{id}.
        notify(f"Failed: {job['url']} — {raw[:150]}")


async def sweep_expired() -> None:
    """Safety net: delete files the client never picked up."""
    while True:
        now = time.time()
        for job_id, job in list(jobs.items()):
            delivered_at = job.get("delivered_at")
            expired = now - job["created_at"] > FILE_TTL_SECONDS or (
                delivered_at is not None and now - delivered_at > DELIVERED_GRACE_SECONDS
            )
            if expired and job["status"] in ("done", "delivered", "error"):
                shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)
                if job["status"] == "done":
                    job["status"] = "expired"
                if now - job["created_at"] > FILE_TTL_SECONDS * 24:
                    del jobs[job_id]  # drop very old metadata
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable must be set")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for leftover in DOWNLOAD_DIR.iterdir():  # clean up after a restart
        shutil.rmtree(leftover, ignore_errors=True)
    task = asyncio.create_task(sweep_expired())
    yield
    task.cancel()


app = FastAPI(title="vidgrab", lifespan=lifespan)


async def extract_url(request: Request) -> str:
    """Pull the target URL from whatever body shape the client sent.

    iOS Shortcuts and shared links occasionally wrap URLs with stray
    backslashes (e.g. ``https:\\/\\/x.com...``), which breaks strict JSON
    parsing. Accepting form/plain-text bodies and stripping backslashes makes
    the endpoint forgiving of that, while still supporting a JSON body.
    """
    # Read the raw body exactly once. Starlette caches it, so the json()/form()
    # helpers below replay this instead of re-consuming the stream (calling
    # them first would leave a later body() read raising "Stream consumed").
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    url = ""

    if "application/json" in content_type:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                url = str(data.get("url", ""))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            url = ""  # fall through to salvage below
    elif "form" in content_type:
        form = await request.form()
        url = str(form.get("url", ""))

    if not url:
        url = request.query_params.get("url", "")
    if not url:
        # Salvage: pull the first URL out of the raw body, tolerating the bad
        # backslash escapes that broke JSON parsing.
        raw = body.decode("utf-8", "replace").replace("\\", "")
        match = re.search(r"https?:[^\s\"']+", raw)
        url = match.group(0) if match else raw

    return url.replace("\\", "").strip()


@app.post("/jobs", dependencies=[Depends(require_key)])
async def create_job(request: Request) -> dict:
    url = await extract_url(request)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must start with http(s)://")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "url": url,
        "status": "queued",
        "progress": 0.0,
        "error": None,
        "filepath": None,
        "files": [],
        "title": None,
        "created_at": time.time(),
        "delivered_at": None,
    }
    executor.submit(download_worker, job_id)
    return {"id": job_id}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def get_job(job_id: str, request: Request) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Absolute, ready-to-fetch URL per video so the client can just iterate the
    # list — no index math or URL building needed. Honor the proxy's forwarded
    # host/scheme (Cloudflare Tunnel) so the URLs are reachable from outside.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    file_urls = [f"{proto}://{host}/jobs/{job_id}/file/{i}" for i in range(len(job["files"]))]
    # Real saved filenames (title + extension), parallel to file_urls, so the
    # iOS Shortcut can name each downloaded file itself — it doesn't read the
    # Content-Disposition header the /file route already sends.
    filenames = [Path(p).name for p in job["files"]]
    # Self-contained record per video. Pairing a URL with its filename by
    # position across two arrays forces the client into index math, which is
    # exactly where the iOS Shortcut breaks (a nested Repeat's index silently
    # binds to the wrong loop). Iterating this list needs no index at all.
    files = [
        {"index": i, "url": url, "filename": name}
        for i, (url, name) in enumerate(zip(file_urls, filenames))
    ]
    return {
        "id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "title": job["title"],
        "error": job["error"],
        "count": len(job["files"]),  # number of downloadable videos in this job
        "files": files,              # one {index, url, filename} per video — iterate this
        "file_urls": file_urls,      # one direct download URL per video
        "filename": filenames[0] if filenames else None,  # primary file's name
        "filenames": filenames,      # one saved filename per video, matches file_urls
    }


def _serve_file(job_id: str, index: int) -> FileResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] not in ("done", "delivered"):
        raise HTTPException(status_code=409, detail=f"job status is {job['status']}")
    if not 0 <= index < len(job["files"]):
        raise HTTPException(status_code=404, detail="file index out of range")
    path = Path(job["files"][index])
    if not path.exists():
        job["status"] = "expired"
        raise HTTPException(status_code=410, detail="file already deleted")

    def mark_delivered() -> None:
        # Files aren't deleted here; the sweeper removes the whole job dir
        # DELIVERED_GRACE_SECONDS later. That window lets the client pull every
        # file of a multi-video post and retry interrupted transfers.
        job["delivered_at"] = time.time()
        job["status"] = "delivered"

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name,
        background=BackgroundTask(mark_delivered),
    )


@app.get("/jobs/{job_id}/file", dependencies=[Depends(require_key)])
def get_file(job_id: str) -> FileResponse:
    # Primary (largest) file — unchanged single-video behavior.
    return _serve_file(job_id, 0)


@app.get("/jobs/{job_id}/file/{index}", dependencies=[Depends(require_key)])
def get_file_indexed(job_id: str, index: int) -> FileResponse:
    # Nth video of a multi-video post (0-based).
    return _serve_file(job_id, index)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "active_jobs": sum(1 for j in jobs.values() if j["status"] in ("queued", "downloading")),
        "proxy": bool(YTDLP_PROXY),
        "cookies": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
    }
