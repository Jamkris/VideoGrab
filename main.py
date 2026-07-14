"""vidgrab — self-hosted video download relay.

Accepts a page URL, downloads its video with yt-dlp on the server,
serves the file to the client exactly once, then deletes it.
Nothing is kept on disk beyond FILE_TTL_SECONDS as a safety net.

Endpoints (all require X-API-Key header):
    POST /jobs            url via JSON body, form field, ?url=, or raw body
                          -> {"id": "..."}
    GET  /jobs/{id}       -> {"status": "queued|downloading|done|error|delivered|expired", "progress": 42.0, ...}
    GET  /jobs/{id}/file  -> the video file (deleted from server after transfer)
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
    """Download `url` with yt-dlp, escalating through three strategies:

    1. Plain generic/site extractor.
    2. Chrome impersonation, for sites that reject non-browser TLS clients.
    3. Resolver fallback, for pages that hide an HLS stream in a nested player
       iframe with disguised extensions — we scrape the real stream URL and
       feed it back to yt-dlp with the page as Referer.
    """
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as first_err:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        try:
            impersonated = {**opts, "impersonate": ImpersonateTarget("chrome")}
            with yt_dlp.YoutubeDL(impersonated) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError:
            stream = resolve_stream(url, proxy=opts.get("proxy", ""))
            if stream is None:
                raise first_err
            hls_opts = {
                **opts,
                "hls_prefer_native": True,
                "http_headers": {"Referer": url, "User-Agent": BROWSER_UA},
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
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    try:
        info = run_ytdlp(job["url"], opts)
        files = sorted(out_dir.iterdir(), key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            raise RuntimeError("download produced no file")
        job["filepath"] = str(files[0])
        job["title"] = (info or {}).get("title", "video")
        job["progress"] = 100.0
        job["status"] = "done"
        notify(f"Ready: {job['title']}")
    except Exception as exc:  # yt-dlp raises many exception types
        job["status"] = "error"
        job["error"] = str(exc)[:500]
        shutil.rmtree(out_dir, ignore_errors=True)
        notify(f"Failed: {job['url']} — {job['error'][:100]}")


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
    content_type = request.headers.get("content-type", "")
    url = ""
    if "application/json" in content_type:
        try:
            data = await request.json()
            if isinstance(data, dict):
                url = str(data.get("url", ""))
        except (json.JSONDecodeError, ValueError):
            url = ""  # fall through to raw-body salvage below
    elif "form" in content_type:
        form = await request.form()
        url = str(form.get("url", ""))

    if not url:
        url = request.query_params.get("url", "")
    if not url:
        # Last resort: treat the raw body as the URL, or dig it out of a
        # body that failed JSON parsing because of bad escapes.
        raw = (await request.body()).decode("utf-8", "replace")
        match = re.search(r"https?:[^\s\"']+", raw.replace("\\", ""))
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
        "title": None,
        "created_at": time.time(),
        "delivered_at": None,
    }
    executor.submit(download_worker, job_id)
    return {"id": job_id}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "title": job["title"],
        "error": job["error"],
    }


@app.get("/jobs/{job_id}/file", dependencies=[Depends(require_key)])
def get_file(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] not in ("done", "delivered"):
        raise HTTPException(status_code=409, detail=f"job status is {job['status']}")
    path = Path(job["filepath"])
    if not path.exists():
        job["status"] = "expired"
        raise HTTPException(status_code=410, detail="file already deleted")

    def mark_delivered() -> None:
        # Not deleted yet: the sweeper removes it DELIVERED_GRACE_SECONDS
        # later, leaving a retry window for interrupted transfers.
        job["delivered_at"] = time.time()
        job["status"] = "delivered"

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name,
        background=BackgroundTask(mark_delivered),
    )


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "active_jobs": sum(1 for j in jobs.values() if j["status"] in ("queued", "downloading")),
        "proxy": bool(YTDLP_PROXY),
        "cookies": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
    }
