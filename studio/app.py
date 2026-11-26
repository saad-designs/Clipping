"""PC dashboard: http://localhost:8790

    python -m studio app            (Windows: double-click "Start Studio.bat")

Campaigns (brief, rules, logo/intro/outro/font, channels) live here and get sent to the
Oracle server. Captured moments come back, get edited by the director + renderer, and wait
for you in Review. Approve -> Ready to upload (with the caption text).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from studio.client import Client, ServerError
from studio.editor.facecam import sample_frames
from studio.store import Store
from studio.worker import Worker

log = logging.getLogger("studio.app")
WEB = Path(__file__).parent / "web" / "index.html"


class ReEdit(BaseModel):
    notes: str


class PlanEdit(BaseModel):
    hook_text: str | None = None
    caption_style: str | None = None
    layout: str | None = None
    post_title: str | None = None
    post_caption: str | None = None
    hashtags: list[str] | None = None
    rerender: bool = True


def mask(key: str) -> str:
    return (key[:7] + "…" + key[-4:]) if key and len(key) > 12 else ("set" if key else "")


WATCHER_KEYS = ("local_watcher", "twitch_client_id", "twitch_client_secret", "max_recording", "quality")


def create_app(store: Store, worker: Worker | None = None, start_worker: bool = True, local=None) -> FastAPI:
    """`local`: a LocalWatcher to run the stream watcher on this PC (one-PC mode), or None."""
    worker = worker or Worker(store)

    @asynccontextmanager
    async def lifespan(_app):
        if local is not None and store.settings().get("local_watcher"):
            local.start()
        if start_worker:
            worker.start()
        yield
        worker.stop()
        if local is not None:
            local.stop()

    def local_on() -> bool:
        return local is not None and bool(store.settings().get("local_watcher"))

    def channels_changed(before: list) -> None:
        from studio.local_watcher import campaign_payload
        if local_on() and campaign_payload(store.campaigns()) != before:
            local.restart()

    app = FastAPI(title="Clipping Studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.worker = store, worker

    def get_clip(cid: int) -> dict:
        c = store.clip(cid)
        if not c:
            raise HTTPException(404, "no such clip")
        return c

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(WEB.read_text(encoding="utf-8"))

    # ---- status / settings ------------------------------------------------------
    @app.get("/api/state")
    def state():
        return {"counts": store.counts(), "worker": worker.status, "current": worker.current,
                "last_sync": worker.last_sync, "jobs": len(worker.jobs),
                "ai": bool(store.settings().get("anthropic_api_key")) and store.settings().get("director") == "ai",
                "director": store.settings().get("director", "free"),
                "output_dir": str(store.output_dir())}

    @app.get("/api/settings")
    def get_settings():
        s = store.settings()
        return {**s, "anthropic_api_key": mask(s.get("anthropic_api_key", "")), "token": mask(s.get("token", "")),
                "twitch_client_secret": mask(s.get("twitch_client_secret", "")), "local_token": "",
                "local_available": local is not None}

    @app.put("/api/settings")
    def put_settings(values: dict):
        for secret in ("anthropic_api_key", "token", "twitch_client_secret"):   # masked values coming back aren't saved
            if secret in values and ("…" in str(values[secret]) or values[secret] == "set"):
                values.pop(secret)
        values.pop("local_token", None)
        before = {k: store.settings().get(k) for k in WATCHER_KEYS}
        store.save_settings(values)
        after = {k: store.settings().get(k) for k in WATCHER_KEYS}
        if local is not None and before != after:
            if after["local_watcher"]:
                local.start()
                local.restart()
            else:
                local.stop()
        worker.wake()
        return get_settings()

    @app.get("/api/local-watcher")
    def local_status():
        if not local_on():
            return {"enabled": False}
        return {"enabled": True, **local.status()}

    @app.post("/api/sync")
    def sync_now():
        if not store.settings().get("server_url"):
            raise HTTPException(400, "Set the server address in Settings first.")
        return worker.sync_once()

    @app.get("/api/server/health")
    def server_health():
        s = store.settings()
        if not s.get("server_url"):
            return {"connected": False, "error": "No server address set (Settings)."}
        try:
            return {"connected": True, **Client(s["server_url"], s["token"], timeout=10).get("/api/health")}
        except ServerError as e:
            return {"connected": False, "error": str(e)}

    # ---- campaigns --------------------------------------------------------------
    @app.get("/api/campaigns")
    def campaigns():
        return store.campaigns()

    @app.post("/api/campaigns")
    def save_campaign(data: dict):
        from studio.local_watcher import campaign_payload
        before = campaign_payload(store.campaigns())
        try:
            saved = store.save_campaign(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        channels_changed(before)        # one-PC mode: the watcher picks up new streamers right away
        return saved

    @app.delete("/api/campaigns/{slug}")
    def delete_campaign(slug: str):
        from studio.local_watcher import campaign_payload
        before = campaign_payload(store.campaigns())
        store.delete_campaign(slug)
        channels_changed(before)
        return {"ok": True}

    @app.post("/api/campaigns/{slug}/asset")
    async def upload_asset(slug: str, kind: str = Form(...), file: UploadFile = File(...)):
        if kind not in ("logo", "intro", "outro", "font"):
            raise HTTPException(400, "kind must be logo, intro, outro or font")
        try:
            return store.save_asset(slug, kind, file.filename or kind, await file.read())
        except KeyError:
            raise HTTPException(404, "no such campaign")

    @app.delete("/api/campaigns/{slug}/asset/{kind}")
    def remove_asset(slug: str, kind: str):
        c = store.campaign(slug) or {}
        if c.get(f"{kind}_path"):
            Path(c[f"{kind}_path"]).unlink(missing_ok=True)
        c[f"{kind}_path"] = None
        return store.save_campaign(c)

    @app.get("/api/campaigns/{slug}/asset/{kind}")
    def get_asset(slug: str, kind: str):
        c = store.campaign(slug) or {}
        p = c.get(f"{kind}_path")
        if not p or not Path(p).exists():
            raise HTTPException(404, "no file")
        return FileResponse(p)

    @app.post("/api/campaigns/push")
    def push_campaigns():
        """Send every campaign's channels and rules to the Oracle server (it restarts itself)."""
        if local_on():
            local.restart()
            return {"ok": True, "server": "this PC"}
        s = store.settings()
        if not s.get("server_url"):
            raise HTTPException(400, "Set the server address in Settings first.")
        payload = [{"name": c["name"], "active": c["active"], "min_seconds": c["min_seconds"],
                    "max_seconds": c["max_seconds"], "required_text": c["required_text"],
                    "extra_hype_tokens": c.get("extra_hype_tokens") or [],
                    "channels": [{"platform": ch["platform"], "login": ch["login"]} for ch in c["channels"]]}
                   for c in store.campaigns()]
        try:
            client = Client(s["server_url"], s["token"])
            with client._req("PUT", "/api/campaigns", {"campaigns": payload}) as r:
                return {"ok": True, "server": r.read().decode()[:300]}
        except ServerError as e:
            raise HTTPException(502, str(e))

    @app.get("/api/campaigns/{slug}/frame")
    def channel_frame(slug: str, channel: str):
        """A frame from the latest clip of this channel, to draw the webcam box on."""
        c = store.campaign(slug)
        clips = [x for x in store.clips() if x["channel"] == channel and (not c or x["campaign"] == c["name"])]
        clips = clips or [x for x in store.clips() if x["channel"] == channel]
        if not clips or not Path(clips[0]["src_path"]).exists():
            raise HTTPException(404, "No clip from this channel yet. Once one is captured, you can draw the webcam box.")
        import cv2
        frames = sample_frames(clips[0]["src_path"], 3)
        ok, jpg = cv2.imencode(".jpg", frames[len(frames) // 2], [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(jpg.tobytes(), media_type="image/jpeg")

    # ---- clips ------------------------------------------------------------------
    @app.get("/api/clips")
    def clips(status: str | None = None):
        out = []
        for c in store.clips(status):
            c.pop("words", None)
            c["has_video"] = bool(c.get("out_path")) and Path(c["out_path"]).exists()
            out.append(c)
        return out

    @app.get("/api/clips/{cid}/video")
    def clip_video(cid: int):
        c = get_clip(cid)
        if not c.get("out_path") or not Path(c["out_path"]).exists():
            raise HTTPException(404, "not rendered yet")
        return FileResponse(c["out_path"], media_type="video/mp4")

    @app.get("/api/clips/{cid}/source")
    def clip_source(cid: int):
        c = get_clip(cid)
        return FileResponse(c["src_path"], media_type="video/mp4")

    @app.post("/api/clips/{cid}/approve")
    def approve(cid: int):
        if get_clip(cid)["status"] != "review":
            raise HTTPException(409, "only clips in review can be approved")
        return worker.approve(cid)

    @app.post("/api/clips/{cid}/reject")
    def reject(cid: int):
        get_clip(cid)
        store.update_clip(cid, status="rejected")
        return store.clip(cid)

    @app.post("/api/clips/{cid}/restore")
    def restore(cid: int):
        c = get_clip(cid)
        store.update_clip(cid, status="review" if c.get("out_path") and Path(c["out_path"]).exists() else "queued")
        worker.wake()
        return store.clip(cid)

    @app.post("/api/clips/{cid}/reedit")
    def reedit(cid: int, body: ReEdit):
        get_clip(cid)
        store.update_clip(cid, status="queued", notes=body.notes.strip(), lock_plan=0)
        worker.wake()
        return store.clip(cid)

    @app.post("/api/clips/{cid}/retry")
    def retry(cid: int):
        get_clip(cid)
        store.update_clip(cid, status="queued", error=None)
        worker.wake()
        return store.clip(cid)

    @app.post("/api/clips/{cid}/edit")
    def edit_plan(cid: int, body: PlanEdit):
        """Change the hook / style / layout / post text yourself. Re-renders without the director."""
        c = get_clip(cid)
        plan = dict(c["plan"] or {})
        if not plan:
            raise HTTPException(409, "clip hasn't been planned yet")
        if body.hook_text is not None:
            plan["hook"] = {**(plan.get("hook") or {"seconds": 2.5}), "text": body.hook_text.strip()} if body.hook_text.strip() else None
        if body.caption_style:
            plan["caption_style"] = body.caption_style
        if body.layout:
            plan["layout"] = body.layout
        post = dict(plan.get("post") or {})
        if body.post_title is not None:
            post["title"] = body.post_title
        if body.post_caption is not None:
            post["caption"] = body.post_caption
        if body.hashtags is not None:
            post["hashtags"] = body.hashtags
        plan["post"] = post
        visual_change = body.hook_text is not None or body.caption_style or body.layout
        if body.rerender and visual_change:
            store.update_clip(cid, plan=plan, lock_plan=1, status="queued")
            worker.wake()
        else:
            store.update_clip(cid, plan=plan)
            if c["status"] == "approved" and c.get("out_path"):    # keep the caption file in sync
                worker_caption(store, cid)
        return store.clip(cid)

    @app.post("/api/clips/{cid}/posted")
    def posted(cid: int):
        get_clip(cid)
        return worker.posted(cid)

    @app.post("/api/clips/{cid}/show")
    def show_in_folder(cid: int):
        p = Path(get_clip(cid).get("out_path") or "")
        if not p.exists():
            raise HTTPException(404, "file not found")
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except OSError as e:
            raise HTTPException(500, str(e))
        return {"ok": True, "path": str(p)}

    # ---- adding videos by hand -------------------------------------------------
    @app.post("/api/ingest")
    async def ingest(campaign: str = Form(...), url: str = Form(""), top: int = Form(5),
                     whole: bool = Form(False), file: UploadFile | None = File(None)):
        """A VOD/video link or an uploaded file. Long videos: the best `top` moments are picked.
        `whole=true`: use the uploaded clip as-is (already a short clip)."""
        if not store.campaign(campaign):
            raise HTTPException(400, "pick a campaign")
        source, label = url.strip(), url.strip()
        if file is not None and file.filename:
            dest = store.root / "uploads" / Path(file.filename).name
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(await file.read())
            source, label = str(dest), file.filename
            if whole:
                camp = store.campaign(campaign)
                cid = store.add_clip(camp["name"], str(dest), "upload", "upload", None, {})
                worker.wake()
                return {"queued": "clip", "id": cid}
        if not source:
            raise HTTPException(400, "paste a link or choose a file")
        worker.add_job({"campaign": campaign, "source": source, "label": label, "top": max(1, min(top, 15))})
        return {"queued": "job"}

    return app


def worker_caption(store: Store, cid: int) -> None:
    c = store.clip(cid)
    post = (c["plan"] or {}).get("post") or {}
    text = "\n\n".join(x for x in [post.get("title", ""), post.get("caption", ""), " ".join(post.get("hashtags") or [])] if x)
    Path(c["out_path"]).with_suffix(".caption.txt").write_text(text + "\n", encoding="utf-8")


def main(root: str = "studio_data", port: int = 8790, open_browser: bool = True) -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    from studio.local_watcher import LocalWatcher, keep_awake
    store = Store(Path(root).resolve())
    _import_yaml_settings(store)
    local = LocalWatcher(store)
    if keep_awake(True):
        print("  This PC won't go to sleep while Studio is open (the screen can still turn off).")
    url = f"http://localhost:{port}"
    print(f"\n  Clipping Studio is running: {url}\n  Keep this window open. Close it to stop.\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(store, local=local), host="127.0.0.1", port=port, log_level="warning")


def _import_yaml_settings(store: Store) -> None:
    """First run: take server/token/key from an existing studio.yaml, if there is one."""
    s = store.settings()
    if s.get("local_watcher") or s.get("server_url"):
        return
    try:
        from studio import config
        y = config.load("studio.yaml")
        if y.get("server_url") and "127.0.0.1" not in y["server_url"]:
            store.save_settings({"server_url": y["server_url"], "token": y.get("token", ""),
                                 "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", "")})
    except Exception:
        pass
