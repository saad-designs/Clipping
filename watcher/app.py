"""Watcher entry point.  Run:  python -m watcher.app

Chat (Twitch / Kick / YouTube) → hype scorer → moments in data/watcher.db
Live channels are recorded into a rolling buffer; each moment gets an .mp4 in data/clips/.
The review dashboard + API for Studio run on http://<server>:8787/.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import shutil
import time
from datetime import datetime
from pathlib import Path

from shared import config
from watcher import audio
from watcher.cutter import Cutter
from watcher.db import DB
from watcher.platforms.base import Platform
from watcher.platforms.kick import Kick
from watcher.platforms.twitch import Twitch, clip_center
from watcher.platforms.youtube import YouTube
from watcher.recorder import RecorderPool, SourceCmd, fake_cmd
from watcher.scorer import ChannelScorer, Moment

log = logging.getLogger("watcher")
RESTART_EXIT_CODE = 3   # "restart me": used on Windows after campaigns change


class Watcher:
    def __init__(self, settings: config.Settings, record: bool = True, source_cmd: SourceCmd | None = None):
        self.s = settings
        rc = settings.recorder
        self.db = DB(settings.data_dir / "watcher.db")
        self.channels = settings.active_channels()
        self.scorers: dict[str, ChannelScorer] = {}
        for key, (_ch, camps) in self.channels.items():
            extra = {t for c in camps for t in c.extra_hype_tokens}
            self.scorers[key] = ChannelScorer(key, settings.scorer, extra)

        by_platform: dict[str, list[str]] = {}
        for ch, _ in self.channels.values():
            by_platform.setdefault(ch.platform, []).append(ch.login)
        self.platforms: dict[str, Platform] = {}
        for name, logins in by_platform.items():
            if name == "twitch":
                p = Twitch(logins, self.on_message, settings.twitch_client_id, settings.twitch_client_secret)
            elif name == "kick":
                p = Kick(logins, self.on_message, settings.kick.pusher_key, settings.kick.pusher_cluster)
            elif name == "youtube":
                p = YouTube(logins, self.on_message, settings.youtube.cookies_file)
            else:
                log.error("unknown platform %r in campaigns.yaml (use twitch, kick or youtube)", name)
                continue
            self.platforms[name] = p
        # platform -> {login: info} of live channels, or None when that platform can't tell
        self.live: dict[str, dict[str, dict] | None] = {name: None for name in self.platforms}

        self.record = record
        self.low_disk = False
        buffer_root = settings.data_dir / "buffer"
        self.clips_root = settings.data_dir / "clips"
        self.cutter = Cutter(self.db, buffer_root, self.clips_root, rc.pre_roll, rc.post_roll, rc.segment_seconds)
        self.pool = RecorderPool(buffer_root, source_cmd or self._record_cmd, rc.segment_seconds,
                                 self.on_segment, rc.max_concurrent)
        self._audio_slots = asyncio.Semaphore(4)
        self._seen_viewer_clips: set[str] = set()
        self.started = time.time()
        self.restart = False
        self._server = None

    # ---- helpers -------------------------------------------------------------------
    def _record_cmd(self, channel: str) -> list[str]:
        platform, login = channel.split(":", 1)
        info = (self.live.get(platform) or {}).get(login)
        return self.platforms[platform].record_cmd(login, info, self.s.recorder.quality)

    def is_live(self, channel: str) -> bool | None:
        platform, login = channel.split(":", 1)
        live = self.live.get(platform)
        return None if live is None else login in live

    def start_recording(self, channel: str) -> None:
        if not self.record:
            return
        if self.low_disk:
            log.warning("disk almost full: not starting recorder for %s", channel)
            return
        self.pool.start(channel)

    # ---- moments -------------------------------------------------------------------
    def _record(self, moments: list[Moment]) -> None:
        for m in moments:
            camps = [c.name for c in self.channels[m.channel][1]]
            mid = self.db.add_moment(m.channel, camps, m.at, m.score, m.signals)
            log.info("🔥 MOMENT #%d  %s  at %s  score=%.1f  %s", mid, m.channel,
                     datetime.fromtimestamp(m.at).strftime("%H:%M:%S"), m.score, m.signals)
            if self.pool.is_recording(m.channel):
                self.cutter.enqueue(mid, m.channel, m.at)

    def on_message(self, platform: str, login: str, ts: float, user: str, text: str) -> None:
        key = f"{platform}:{login}"
        sc = self.scorers.get(key)
        if sc is None or self.is_live(key) is False:
            return
        self._record(sc.add_message(ts, user, text))

    # ---- audio ---------------------------------------------------------------------
    def on_segment(self, channel: str, path: Path, start: float) -> None:
        asyncio.get_running_loop().create_task(self._analyze(channel, path, start))

    async def _analyze(self, channel: str, path: Path, start: float) -> None:
        async with self._audio_slots:
            try:
                readings = await asyncio.to_thread(audio.analyze, path, start)
            except Exception as e:
                log.debug("audio analysis failed for %s: %s", path, e)
                return
        sc = self.scorers.get(channel)
        if sc:
            for ts, lufs in readings:
                sc.add_audio(ts, lufs)

    # ---- loops ---------------------------------------------------------------------
    async def tick_loop(self) -> None:
        while True:
            now = time.time()
            for sc in self.scorers.values():
                self._record(sc.tick(now))
            await asyncio.sleep(1)

    def disk_free_gb(self) -> float:
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.s.data_dir).free / 1e9

    async def cleanup_loop(self) -> None:
        rc = self.s.recorder
        while True:
            self.pool.cleanup(rc.buffer_minutes * 60, self.cutter.oldest_needed)
            cutoff = time.time() - rc.clip_retention_days * 86400
            if self.clips_root.exists():
                for clip in self.clips_root.rglob("*.mp4"):
                    if clip.stat().st_mtime < cutoff:
                        clip.unlink(missing_ok=True)
            # Disk guard: stop growing and free space before the VM fills up and everything breaks.
            self.low_disk = self.disk_free_gb() < rc.min_free_gb
            if self.low_disk:
                for row in self.db.prunable_clips(10):
                    Path(row["clip_path"]).unlink(missing_ok=True)
                    self.db.clear_clip(row["id"])
                log.warning("low disk (%.1f GB free): pruned old clips, new recordings paused", self.disk_free_gb())
            await asyncio.sleep(30)

    async def live_loop(self, name: str, platform: Platform) -> None:
        while True:
            try:
                live = await platform.live_status()
            except Exception as e:  # network blips must never kill the watcher
                log.warning("%s live check failed: %s", name, e)
                await asyncio.sleep(60)
                continue
            if live is None:   # platform can't tell (e.g. no Twitch keys): try recording everything
                for login in platform.logins:
                    self.start_recording(f"{name}:{login}")
                return
            previous = self.live.get(name) or {}
            for gone in set(previous) - set(live):
                key = f"{name}:{gone}"
                log.info("offline: %s", key)
                self.scorers[key].reset()
                platform.on_live_change(gone, None)
                await self.pool.stop(key)
            for new in set(live) - set(previous):
                log.info("LIVE: %s:%s  %s", name, new, live[new].get("title", ""))
                platform.on_live_change(new, live[new])
            self.live[name] = live
            for login in live:
                self.start_recording(f"{name}:{login}")   # no-op if already recording
            await asyncio.sleep(60)

    async def viewer_clips_loop(self, tw: Twitch) -> None:
        """Twitch viewers' own clips: badge matching moments, and catch moments the detector missed."""
        if not tw.helix:
            return
        while True:
            await asyncio.sleep(180)
            for login, info in (self.live.get("twitch") or {}).items():
                key = f"twitch:{login}"
                try:
                    clips = await tw.helix.recent_clips(info["user_id"], time.time() - 900)
                except Exception as e:
                    log.debug("clips lookup failed for %s: %s", login, e)
                    continue
                for clip in clips:
                    if clip["id"] in self._seen_viewer_clips:
                        continue
                    self._seen_viewer_clips.add(clip["id"])
                    center = clip_center(clip)
                    if self.db.add_viewer_clips(key, center, window=45):
                        continue
                    # Viewers clipped something we didn't flag: add it as a moment of its own.
                    camps = [c.name for c in self.channels[key][1]]
                    mid = self.db.add_moment(key, camps, center, 0.0,
                                             {"source": "viewer_clip", "title": clip.get("title", "")})
                    self.db.add_viewer_clips(key, center, window=1)
                    log.info("🎬 viewer clip with no moment → added #%d for %s", mid, key)
                    if self.pool.is_recording(key):
                        self.cutter.enqueue(mid, key, center)

    def health(self) -> dict:
        free = self.disk_free_gb()
        recent = self.db.counts_since(time.time() - 3600)
        chans = []
        for key, (ch, camps) in self.channels.items():
            p = self.platforms.get(ch.platform)
            info = (self.live.get(ch.platform) or {}).get(ch.login) or {}
            chans.append({"channel": key, "live": self.is_live(key), "recording": self.pool.is_recording(key),
                          "chat": bool(p and ch.login in p.chat_connected), "title": info.get("title", ""),
                          "viewers": info.get("viewers"), "moments_1h": recent.get(key, 0),
                          "campaigns": [c.name for c in camps]})
        return {"disk_free_gb": round(free, 1), "low_disk": self.low_disk,
                "recording": len(self.pool.recording()), "max_recording": self.s.recorder.max_concurrent,
                "uptime_s": int(time.time() - self.started), "channels": chans}

    async def run(self, api: bool = True) -> None:
        if not self.platforms:
            raise SystemExit("No active channels in campaigns.yaml")
        tasks = [self.tick_loop()]
        for name, p in self.platforms.items():
            tasks += [p.run_chat(), self.live_loop(name, p)]
        if "twitch" in self.platforms:
            if not self.platforms["twitch"].helix:
                log.info("no twitch client id set: live checks and viewer-clip badges are off")
            tasks.append(self.viewer_clips_loop(self.platforms["twitch"]))
        if self.record:
            tasks += [self.cutter.run(), self.cleanup_loop()]
        log.info("watching %d channels on %s (recording %s)", len(self.channels),
                 ", ".join(self.platforms), "on" if self.record else "off")
        background = [asyncio.ensure_future(t) for t in tasks]
        for t in background:
            t.add_done_callback(_log_crash)
        try:
            if api:
                await self.serve_api()   # returns on Ctrl+C / SIGTERM
            else:
                await asyncio.Event().wait()
        finally:
            for t in background:
                t.cancel()
            await self.pool.stop_all()

    def request_restart(self) -> None:
        """Stop cleanly (recorders included), then main() starts a fresh process with the new campaigns."""
        self.restart = True
        if self._server is not None:
            asyncio.get_running_loop().call_later(0.5, setattr, self._server, "should_exit", True)

    async def serve_api(self) -> None:
        import uvicorn
        from watcher.api import create_app
        token = ensure_token(self.s)
        cfg = uvicorn.Config(create_app(self, token), host=self.s.api.host, port=self.s.api.port,
                             log_level="warning", lifespan="off")
        log.info("dashboard: http://<this-server>:%d/?token=%s", self.s.api.port, token)
        self._server = uvicorn.Server(cfg)
        await self._server.serve()


def _log_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("background task crashed", exc_info=task.exception())


def ensure_token(s: config.Settings) -> str:
    """Use api.token from settings, or create one once and keep it in data/api_token.txt."""
    if s.api.token:
        return s.api.token
    f = s.data_dir / "api_token.txt"
    if f.exists():
        return f.read_text().strip()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    f.write_text(tok)
    return tok


def main() -> None:
    p = argparse.ArgumentParser(description="Clipping watcher")
    p.add_argument("--root", default=".", help="folder containing settings.yaml / campaigns.yaml")
    p.add_argument("--channels", nargs="*", help="quick test: channels to watch (twitch name, or kick:name / youtube:handle)")
    p.add_argument("--no-record", action="store_true", help="chat-only: no stream recording or clips")
    p.add_argument("--no-api", action="store_true", help="don't run the dashboard/API")
    p.add_argument("--fake-source", help="record this local video file (looped) instead of the real stream")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    s = config.load(args.root)
    if args.channels:
        refs = [config._channel({"platform": c.split(":")[0] if ":" in c else "twitch", "login": c.split(":")[-1]})
                for c in args.channels]
        s.campaigns = [config.Campaign("adhoc", True, refs)]
    source = fake_cmd(args.fake_source) if args.fake_source else None

    holder = {}

    async def go():
        holder["w"] = Watcher(s, record=not args.no_record, source_cmd=source)
        await holder["w"].run(api=not args.no_api)
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        return
    if holder.get("w") and holder["w"].restart:
        log.info("campaigns changed: restarting")
        if sys.platform == "win32":
            sys.exit(RESTART_EXIT_CODE)    # Clipping Studio relaunches it (execv is unreliable on Windows)
        os.execv(sys.executable, [sys.executable, "-m", "watcher.app", *sys.argv[1:]])


if __name__ == "__main__":
    main()
