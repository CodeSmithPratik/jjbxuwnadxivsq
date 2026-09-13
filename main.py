"""POST {url} -> best-quality mp4 stream. Ephemeral disk; nothing persisted."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(docs_url=None, redoc_url=None)  # no docs UI

URL_RE = re.compile(r"^https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_-]+/?(\?.*)?$")
MAX_BYTES = 150 * 1024 * 1024  # refuse >150MB merges (free-tier disk)
MIN_GAP = 3.0  # seconds between fetches: serial + paced, datacenter-IP friendly

_gate = threading.Lock()
_last_fetch = 0.0


def _cookies_file() -> str | None:
    """Netscape cookies from IG_COOKIES env (spare account, never main)."""
    raw = os.getenv("IG_COOKIES", "").strip()
    if not raw:
        return None
    path = "/tmp/ig_cookies.txt"
    with open(path, "w") as fh:
        fh.write(raw if raw.endswith("\n") else raw + "\n")
    os.chmod(path, 0o600)
    return path


class FetchReq(BaseModel):
    url: str


class BatchReq(BaseModel):
    urls: list[str]


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
MAX_URLS = 50  # per job; serial + paced so free-tier timeouts never bite


def _mint(url: str) -> dict:
    """One URL -> signed CDN urls, no download. Raises HTTPException."""
    url = (url or "").strip()
    if not URL_RE.match(url):
        return {"url": url, "cdn": [], "error": "only instagram reel/post urls"}
    opts = {
        "format": "bv*+ba/b",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "sleep_requests": 2,
    }
    if proxy := os.getenv("RELAY_PROXY"):
        opts["proxy"] = proxy
    if cookies := _cookies_file():
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        fmts = info.get("requested_formats") or ([info] if info.get("url") else [])
        cdn = [f["url"] for f in fmts if f.get("url")]
        return {"url": url, "cdn": cdn,
                "error": None if cdn else "empty media response (deleted/private?)"}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "cdn": [], "error": str(exc)[:160]}


def _run_job(job_id: str, urls: list[str]) -> None:
    global _last_fetch
    for i, u in enumerate(urls):
        with _gate:
            wait = MIN_GAP - (time.time() - _last_fetch)
            if wait > 0:
                time.sleep(wait)
            item = _mint(u)
            _last_fetch = time.time()
        with _jobs_lock:
            _jobs[job_id]["items"].append(item)
            _jobs[job_id]["done"] = i + 1
    with _jobs_lock:
        _jobs[job_id]["state"] = "done"


@app.post("/jobs")
def create_job(req: BatchReq):
    urls = [u.strip() for u in (req.urls or []) if u and u.strip()][:MAX_URLS]
    if not urls:
        raise HTTPException(400, "provide 1-50 urls")
    job_id = os.urandom(8).hex()
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "total": len(urls), "done": 0,
                         "items": [], "note": "cdn urls expire in ~1-6 days; download immediately"}
        while len(_jobs) > 20:  # prune oldest (ephemeral by design)
            _jobs.pop(next(iter(_jobs)))
    threading.Thread(target=_run_job, args=(job_id, urls), daemon=True).start()
    return {"job_id": job_id, "total": len(urls)}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown or expired job (server sleeps = jobs reset)")
    return job


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/fetch")
def fetch(req: FetchReq, bg: BackgroundTasks):
    url = (req.url or "").strip()
    if not URL_RE.match(url):
        raise HTTPException(400, "only instagram reel/post urls")
    tmp = tempfile.mkdtemp(prefix="r")
    bg.add_task(shutil.rmtree, tmp, True)
    out = os.path.join(tmp, "%(id)s.%(ext)s")
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": out,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,          # one retry with backoff instead of hammering
        "sleep_requests": 2,
    }
    if proxy := os.getenv("RELAY_PROXY"):
        opts["proxy"] = proxy  # e.g. socks5h://user:pass@host:port
    if cookies := _cookies_file():
        opts["cookiefile"] = cookies  # authenticated = far more leniency
    global _last_fetch
    try:
        with _gate:  # serial queue: one fetch at a time, paced
            wait = MIN_GAP - (time.time() - _last_fetch)
            if wait > 0:
                time.sleep(wait)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                got = ydl.prepare_filename(info)
        finally:
            _last_fetch = time.time()
        base = os.path.splitext(got)[0] + ".mp4"
        path = base if os.path.exists(base) else got
        if os.path.getsize(path) > MAX_BYTES:
            raise HTTPException(413, "file too large for free tier")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - yt-dlp failure surface is broad
        raise HTTPException(502, f"fetch failed: {str(exc)[:160]}")
    return FileResponse(path, filename=os.path.basename(path),
                        media_type="video/mp4", background=bg)
