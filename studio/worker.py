"""Background worker for the PC dashboard.

Loop: pull new captures from the Oracle server -> for each queued clip: transcribe ->
find the webcam -> director plans the edit -> render the vertical video -> "review".
Also runs "Add video" jobs (VOD links / uploaded files) through VOD moment picking.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from studio import director
from studio.client import Client, ServerError
from studio.editor import facecam as facecam_mod
from studio.editor import render as render_mod
from studio.editor import transcribe
from studio.editor.pipeline import probe
from studio.store import Store
from watcher import audio
from studio.sync import safe

log = logging.getLogger("worker")


def clip_stem(clip: dict) -> str:
    when = datetime.fromtimestamp(clip["created"]).strftime("%Y%m%d_%H%M%S")
    chan = safe((clip["channel"] or clip["source"]).replace(":", "_"))
    return f"{chan}_{when}_{clip['id']}"


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self.status = "idle"            # shown on the dashboard
        self.current: int | None = None
        self.last_sync: dict = {}
        self.jobs: list[dict] = []      # pending "Add video" jobs
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sweep: dict = {}
        self._sweep_now = False

    # ---- control --------------------------------------------------------------
    def start(self) -> None:
        self.store.reset_stuck()
        self._thread = threading.Thread(target=self._loop, name="studio-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def add_job(self, job: dict) -> None:
        self.jobs.append(job)
        self.wake()

    def _loop(self) -> None:
        next_sync = next_sweep = 0.0
        while not self._stop.is_set():
            s = self.store.settings()
            if s.get("find_mode", "vods") == "vods":
                if self._sweep_now or time.time() >= next_sweep:
                    self._sweep_now = False
                    r = self.sweep_once()
                    retry = 600 if r.get("errors") and not r.get("vods") else float(s.get("sweep_hours") or 2) * 3600
                    next_sweep = time.time() + retry
            elif s.get("server_url") and time.time() >= next_sync:
                r = self.sync_once()
                # a failed check (e.g. the watcher on this PC is still starting) is retried soon
                next_sync = time.time() + (20 if r.get("error") else float(s.get("poll_minutes") or 3) * 60)
            if self.jobs:
                self._run_job(self.jobs.pop(0))
                continue
            clip = self.store.next_queued()
            if clip:
                self.process(clip)
                continue
            self.status = "idle"
            self._wake.wait(timeout=10)
            self._wake.clear()

    def request_sweep(self) -> None:
        self._sweep_now = True
        self.wake()

    def sweep_once(self) -> dict:
        from studio import sweep
        s = self.store.settings()
        try:
            r = sweep.sweep(self.store, float(s.get("sweep_days") or 3), on_status=lambda m: setattr(self, "status", m))
        except Exception as e:
            log.exception("sweep failed")
            r = {"new_clips": 0, "vods": 0, "errors": [f"{type(e).__name__}: {e}"]}
        self.last_sweep = {**r, "at": time.time()}
        self.status = "idle"
        return r

    # ---- pulling captures from the server -----------------------------------------
    def sync_once(self) -> dict:
        s = self.store.settings()
        result = {"at": time.time(), "new": 0, "error": None}
        try:
            self.status = "checking the server for new captures"
            client = Client(s["server_url"], s["token"])
            local = {c["name"]: c for c in self.store.campaigns()}
            for m in client.get("/api/moments?status=new&has_clip=true&limit=100"):
                camps = [c for c in m["campaigns"] if c in local] or ([next(iter(local))] if len(local) == 1 else [])
                if not camps:
                    continue
                dest = self.store.root / "clips" / f"server_{m['id']}.mp4"
                if not dest.exists():
                    client.download(m["clip_url"], dest)
                meta = {"moment_offset": m.get("moment_offset"), "signals": m.get("signals"), "score": m.get("score"),
                        "at": m.get("at"), "viewer_clips": m.get("viewer_clips", 0)}
                for name in camps:
                    if self.store.add_clip(name, str(dest), m["channel"], "live", m["id"], meta):
                        result["new"] += 1
                client.post(f"/api/moments/{m['id']}/downloaded")
        except ServerError as e:
            result["error"] = str(e)
            log.warning("sync: %s", e)
        except Exception as e:   # never let a bad response kill the worker
            result["error"] = f"{type(e).__name__}: {e}"
            log.exception("sync failed")
        self.last_sync = result
        return result

    # ---- "Add video" jobs -------------------------------------------------------
    def _run_job(self, job: dict) -> None:
        from studio import vod
        camp = self.store.campaign(job["campaign"])
        if not camp:
            return
        self.status = f"finding the best moments in {job.get('label') or job['source']}"
        try:
            cfg = {"output_dir": self.store.root / "vod_work", "vod": {"top": job.get("top", 5), "min_gap_seconds": 90,
                                                                       "whisper_model": "base"}}
            picks = vod.run(job["source"], "picks", cfg, job.get("top", 5),
                            {"min_seconds": camp["min_seconds"], "max_seconds": camp["max_seconds"]},
                            transcribe_no_chat=False)
            for p in picks:
                side = json.loads(p.with_suffix(".json").read_text())
                dest = self.store.root / "clips" / p.name
                shutil.move(str(p), dest)
                p.with_suffix(".json").unlink(missing_ok=True)
                self.store.add_clip(camp["name"], str(dest), side.get("channel") or "vod", "vod", None,
                                    {"moment_offset": side.get("moment_offset"), "score": side.get("score"),
                                     "vod": job["source"], "vod_time": side.get("vod_time")})
        except Exception as e:
            log.exception("add video failed")
            self.last_sync = {**self.last_sync, "job_error": f"{type(e).__name__}: {e}"}

    # ---- editing one clip --------------------------------------------------------
    def process(self, clip: dict) -> None:
        cid = clip["id"]
        self.current = cid
        s = self.store.settings()
        camp = self.store.campaign(clip["campaign"])
        if not camp:
            self.store.update_clip(cid, status="failed", error=f"campaign '{clip['campaign']}' no longer exists")
            return
        self.store.update_clip(cid, status="editing", error=None)
        src = Path(clip["src_path"])
        try:
            duration, has_audio = probe(src)
            words, meta = clip["words"], dict(clip["meta"])
            if words is None:
                self.status = f"#{cid}: transcribing"
                ws, lang = transcribe.transcribe_full(src, s.get("whisper_model") or "small", s.get("language")) \
                    if has_audio else ([], None)
                words = [[round(w.start, 2), round(w.end, 2), w.text] for w in ws]
                meta["language"] = lang
                self.store.update_clip(cid, words=words, meta=meta)
            if has_audio and "loudness" not in meta:       # per-second loudness: finds the real payoff
                self.status = f"#{cid}: measuring the audio"
                try:
                    meta["loudness"] = [[t, round(l, 1)] for t, l in audio.analyze(src, 0, timeout=300)]
                except Exception as e:
                    log.warning("loudness analysis failed: %s", e)
                    meta["loudness"] = []
                self.store.update_clip(cid, meta=meta)
            word_objs = [transcribe.Word(*w) for w in words]

            fc = clip["facecam"]
            if fc is None:
                fc = self._facecam(src, camp, clip["channel"])
                self.store.update_clip(cid, facecam=fc)

            if clip["lock_plan"] and clip["plan"]:
                plan = clip["plan"]
            else:
                self.status = f"#{cid}: director is planning the edit"
                ctx = director.build_context({**meta, "channel": clip["channel"]}, camp, word_objs, fc, duration,
                                             self.store.recent_styles(camp["name"]), clip["notes"], clip["plan"],
                                             meta.get("loudness"), meta.get("language"), self.store.recent_hooks(camp["name"]))
                plan = director.plan(ctx, src, s)

            self.status = f"#{cid}: rendering"
            out_dir = self.store.output_dir() / safe(camp["name"]) / "review"
            out = out_dir / f"{clip_stem(clip)}.mp4"
            render_mod.render(src, plan, out, camp, word_objs, plan.get("facecam") or fc,
                              {"size": 104, "words_per_line": 3, "uppercase": True}, s.get("encoder") or "auto")
            self.store.update_clip(cid, status="review", plan=plan, out_path=str(out), lock_plan=0, notes="")
            log.info("clip #%s ready for review (%s, %s)", cid, plan["layout"], plan.get("source"))
        except Exception as e:
            log.exception("clip #%s failed", cid)
            self.store.update_clip(cid, status="failed", error=f"{type(e).__name__}: {str(e)[-600:]}")
        finally:
            self.current = None

    def _facecam(self, src: Path, camp: dict, channel: str) -> dict:
        """The campaign's per-channel setting wins: a drawn box, 'none', or automatic detection."""
        setting = "auto"
        for ch in camp.get("channels") or []:
            if f"{ch['platform']}:{ch['login']}" == channel:
                setting = ch.get("facecam") or "auto"
        if isinstance(setting, list) and len(setting) == 4:
            return {"kind": "webcam", "cam": setting, "source": "manual"}
        if setting == "none":
            return {"kind": "none", "source": "manual"}
        self.status = "finding the webcam"
        return {**facecam_mod.detect(src).to_dict(), "source": "auto"}

    # ---- review actions ---------------------------------------------------------
    def approve(self, cid: int) -> dict:
        clip = self.store.clip(cid)
        camp = self.store.campaign(clip["campaign"]) or {"name": clip["campaign"]}
        ready = self.store.output_dir() / safe(camp["name"]) / "ready"
        ready.mkdir(parents=True, exist_ok=True)
        src = Path(clip["out_path"])
        dest = ready / src.name
        shutil.move(str(src), dest)
        post = (clip["plan"] or {}).get("post") or {}
        text = "\n\n".join(x for x in [post.get("title", ""), post.get("caption", ""),
                                        " ".join(post.get("hashtags") or [])] if x)
        dest.with_suffix(".caption.txt").write_text(text + "\n", encoding="utf-8")
        self.store.update_clip(cid, status="approved", out_path=str(dest))
        return self.store.clip(cid)

    def posted(self, cid: int) -> dict:
        clip = self.store.clip(cid)
        src = Path(clip["out_path"])
        posted = src.parent.parent / "posted"
        posted.mkdir(parents=True, exist_ok=True)
        for f in (src, src.with_suffix(".caption.txt")):
            if f.exists():
                shutil.move(str(f), posted / f.name)
        self.store.update_clip(cid, status="posted", out_path=str(posted / src.name))
        return self.store.clip(cid)
