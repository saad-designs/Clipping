"""Watcher entry point.  Run:  python -m watcher.app

Twitch chat → hype scorer → moments in data/watcher.db
Live channels are also recorded into a rolling buffer; each moment gets an .mp4 clip
in data/clips/ and the stream's audio loudness feeds the scorer.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from shared import config
from watcher import audio
from watcher.cutter import Cutter
from watcher.db import DB
from watcher.platforms.twitch import TwitchChat, TwitchHelix
from watcher.recorder import RecorderPool, SourceCmd, fake_cmd, streamlink_cmd
from watcher.scorer import ChannelScorer, Moment

log = logging.getLogger("watcher")


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
        self.live: set[str] | None = None  # None = unknown (no Helix creds), treat all as live

        self.record = record
        buffer_root = settings.data_dir / "buffer"
        self.clips_root = settings.data_dir / "clips"
        self.cutter = Cutter(self.db, buffer_root, self.clips_root, rc.pre_roll, rc.post_roll, rc.segment_seconds)
        self.pool = RecorderPool(buffer_root, source_cmd or streamlink_cmd(rc.quality), rc.segment_seconds,
                                 self.on_segment, rc.max_concurrent)
        self._audio_slots = asyncio.Semaphore(4)

    # ---- moments -------------------------------------------------------------------
    def _record(self, moments: list[Moment]) -> None:
        for m in moments:
            camps = [c.name for c in self.channels[m.channel][1]]
            mid = self.db.add_moment(m.channel, camps, m.at, m.score, m.signals)
            log.info("🔥 MOMENT #%d  %s  at %s  score=%.1f  %s", mid, m.channel,
                     datetime.fromtimestamp(m.at).strftime("%H:%M:%S"), m.score, m.signals)
            if self.pool.is_recording(m.channel):
                self.cutter.enqueue(mid, m.channel, m.at)

    def on_twitch_message(self, login: str, ts: float, user: str, text: str) -> None:
        key = f"twitch:{login}"
        sc = self.scorers.get(key)
        if sc is None or (self.live is not None and login not in self.live):
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

    async def cleanup_loop(self) -> None:
        rc = self.s.recorder
        while True:
            self.pool.cleanup(rc.buffer_minutes * 60, self.cutter.oldest_needed)
            cutoff = time.time() - rc.clip_retention_days * 86400
            if self.clips_root.exists():
                for clip in self.clips_root.rglob("*.mp4"):
                    if clip.stat().st_mtime < cutoff:
                        clip.unlink(missing_ok=True)
            await asyncio.sleep(30)

    async def live_loop(self, helix: TwitchHelix, logins: list[str]) -> None:
        while True:
            try:
                live = await helix.live_logins(logins)
                previous = self.live or set()
                for gone in previous - live:
                    log.info("offline: %s", gone)
                    self.scorers[f"twitch:{gone}"].reset()
                    await self.pool.stop(f"twitch:{gone}")
                for new in live - previous:
                    log.info("LIVE: %s", new)
                if self.record:
                    for login in live:
                        self.pool.start(f"twitch:{login}")   # no-op if already recording
                self.live = live
            except Exception as e:  # network blips must never kill the watcher
                log.warning("live check failed: %s", e)
            await asyncio.sleep(60)

    async def run(self) -> None:
        logins = [ch.login for ch, _ in self.channels.values() if ch.platform == "twitch"]
        if not logins:
            raise SystemExit("No active twitch channels in campaigns.yaml")
        tasks = [TwitchChat(logins, self.on_twitch_message).run(), self.tick_loop()]
        if self.record:
            tasks += [self.cutter.run(), self.cleanup_loop()]
        if self.s.twitch_client_id and self.s.twitch_client_secret:
            tasks.append(self.live_loop(TwitchHelix(self.s.twitch_client_id, self.s.twitch_client_secret), logins))
        else:
            log.info("no twitch client id set: skipping live checks (chat still monitored)")
            if self.record:
                # Without live status, keep trying each channel; streamlink exits at once if offline.
                for login in logins:
                    self.pool.start(f"twitch:{login}")
        log.info("watching %d channels (recording %s)", len(logins), "on" if self.record else "off")
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.pool.stop_all()


def main() -> None:
    p = argparse.ArgumentParser(description="Clipping watcher")
    p.add_argument("--root", default=".", help="folder containing settings.yaml / campaigns.yaml")
    p.add_argument("--channels", nargs="*", help="quick test: twitch logins to watch, ignores campaigns.yaml")
    p.add_argument("--no-record", action="store_true", help="chat-only: no stream recording or clips")
    p.add_argument("--fake-source", help="record this local video file (looped) instead of the real stream")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    s = config.load(args.root)
    if args.channels:
        s.campaigns = [config.Campaign("adhoc", True, [config.ChannelRef("twitch", c.lower()) for c in args.channels])]
    source = fake_cmd(args.fake_source) if args.fake_source else None

    async def go():
        await Watcher(s, record=not args.no_record, source_cmd=source).run()
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
