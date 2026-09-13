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
