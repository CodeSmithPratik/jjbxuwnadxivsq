"""POST {url} -> best-quality mp4 stream. Ephemeral disk; nothing persisted."""
from __future__ import annotations

import os
import re
import shutil
import tempfile

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(docs_url=None, redoc_url=None)  # no docs UI

URL_RE = re.compile(r"^https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_-]+/?(\?.*)?$")
MAX_BYTES = 150 * 1024 * 1024  # refuse >150MB merges (free-tier disk)


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
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            got = ydl.prepare_filename(info)
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
