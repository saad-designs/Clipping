"""HTTP API + review dashboard.

Every request needs the token: header `X-Token`, or `?token=` once in the browser
(it's then kept in a cookie). Reach this over Tailscale; don't open the port publicly.
"""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from watcher.db import STATUSES, row_to_dict

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


class MomentUpdate(BaseModel):
    status: str | None = None
    campaigns: list[str] | None = None


def moment_json(m: dict) -> dict:
    m = dict(m)
    m["has_clip"] = bool(m.get("clip_path")) and Path(m["clip_path"]).exists()
    m["clip_url"] = f"/api/clips/{m['id']}" if m["has_clip"] else None
    # seconds into the clip where the moment happens (the dashboard starts playback here)
    m["moment_offset"] = round(m["at"] - m["clip_start"], 1) if m.get("clip_start") else None
    m.pop("clip_path", None)
    return m


def create_app(watcher, token: str) -> FastAPI:
    app = FastAPI(title="Clipping Watcher", docs_url=None, redoc_url=None)
    db = watcher.db

    def auth(request: Request) -> None:
        sent = request.headers.get("x-token") or request.cookies.get("token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(sent, token):
            raise HTTPException(401, "bad or missing token")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        q = request.query_params.get("token")
        if q is not None:
            if not hmac.compare_digest(q, token):
                raise HTTPException(401, "bad token")
            # move the token from the URL into a cookie, so it isn't left in history/screenshots
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("token", token, httponly=True, samesite="strict", max_age=365 * 86400)
            return resp
        auth(request)
        return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))

    @app.get("/api/health", dependencies=[Depends(auth)])
    async def health():
        return watcher.health()

    @app.get("/api/campaigns", dependencies=[Depends(auth)])
    async def campaigns():
        return [c.public() for c in watcher.s.campaigns]

    @app.get("/api/moments", dependencies=[Depends(auth)])
    async def moments(status: str | None = None, limit: int = 200, has_clip: bool = False):
        if status and status not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        return [moment_json(m) for m in db.list(status, min(limit, 1000), has_clip)]

    def _get(mid: int) -> dict:
        row = db.get(mid)
        if not row:
            raise HTTPException(404, "no such moment")
        return row_to_dict(row)

    @app.post("/api/moments/{mid}", dependencies=[Depends(auth)])
    async def update(mid: int, body: MomentUpdate):
        _get(mid)
        if body.status and body.status not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        if body.campaigns is not None:
            known = {c.name for c in watcher.s.campaigns}
            bad = [c for c in body.campaigns if c not in known]
            if bad:
                raise HTTPException(400, f"unknown campaigns: {bad}")
        db.update(mid, body.status, body.campaigns)
        return moment_json(_get(mid))

    @app.put("/api/campaigns", dependencies=[Depends(auth)])
    async def put_campaigns(body: dict):
        """Campaigns from the Studio dashboard: save them, then restart to watch the new channels."""
        from shared import config
        items = body.get("campaigns")
        if not isinstance(items, list):
            raise HTTPException(400, "expected {\"campaigns\": [...]}")
        path = config.write_campaigns(watcher.s.root, items)
        watcher.request_restart()
        n = sum(len(c.get("channels") or []) for c in items)
        return {"ok": True, "saved": str(path), "campaigns": len(items), "channels": n, "restarting": True}

    @app.post("/api/moments/{mid}/downloaded", dependencies=[Depends(auth)])
    async def downloaded(mid: int):
        _get(mid)
        db.update(mid, "downloaded")
        return {"ok": True}

    @app.get("/api/clips/{mid}", dependencies=[Depends(auth)])
    async def clip(mid: int):
        m = _get(mid)
        if not m.get("clip_path") or not Path(m["clip_path"]).exists():
            raise HTTPException(404, "clip not available (not cut yet, or deleted)")
        # FileResponse handles Range requests, so the browser can seek and Studio can resume.
        return FileResponse(m["clip_path"], media_type="video/mp4", filename=Path(m["clip_path"]).name)

    return app
