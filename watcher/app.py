"""Watcher entry point.  Run:  python -m watcher.app

Step 1 scope: Twitch chat → hype scorer → moments saved in data/watcher.db.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime

from shared import config
from watcher.db import DB
from watcher.platforms.twitch import TwitchChat, TwitchHelix
from watcher.scorer import ChannelScorer, Moment

log = logging.getLogger("watcher")


class Watcher:
    def __init__(self, settings: config.Settings):
        self.s = settings
        self.db = DB(settings.data_dir / "watcher.db")
        self.channels = settings.active_channels()
        self.scorers: dict[str, ChannelScorer] = {}
        for key, (_ch, camps) in self.channels.items():
            extra = {t for c in camps for t in c.extra_hype_tokens}
            self.scorers[key] = ChannelScorer(key, settings.scorer, extra)
        self.live: set[str] | None = None  # None = unknown (no Helix creds), treat all as live

    def _record(self, moments: list[Moment]) -> None:
        for m in moments:
            camps = [c.name for c in self.channels[m.channel][1]]
            mid = self.db.add_moment(m.channel, camps, m.at, m.score, m.signals)
            log.info("🔥 MOMENT #%d  %s  at %s  score=%.1f  %s", mid, m.channel,
                     datetime.fromtimestamp(m.at).strftime("%H:%M:%S"), m.score, m.signals)

    def on_twitch_message(self, login: str, ts: float, user: str, text: str) -> None:
        key = f"twitch:{login}"
        sc = self.scorers.get(key)
        if sc is None or (self.live is not None and login not in self.live):
            return
        self._record(sc.add_message(ts, user, text))

    async def tick_loop(self) -> None:
        while True:
            now = time.time()
            for sc in self.scorers.values():
                self._record(sc.tick(now))
            await asyncio.sleep(1)

    async def live_loop(self, helix: TwitchHelix, logins: list[str]) -> None:
        while True:
            try:
                live = await helix.live_logins(logins)
                if self.live is not None:
                    for gone in self.live - live:
                        log.info("offline: %s", gone)
                        self.scorers[f"twitch:{gone}"].reset()
                    for new in live - self.live:
                        log.info("LIVE: %s", new)
                self.live = live
            except Exception as e:  # network blips must never kill the watcher
                log.warning("live check failed: %s", e)
            await asyncio.sleep(60)

    async def run(self) -> None:
        logins = [ch.login for ch, _ in self.channels.values() if ch.platform == "twitch"]
        if not logins:
            raise SystemExit("No active twitch channels in campaigns.yaml")
        tasks = [TwitchChat(logins, self.on_twitch_message).run(), self.tick_loop()]
        if self.s.twitch_client_id and self.s.twitch_client_secret:
            tasks.append(self.live_loop(TwitchHelix(self.s.twitch_client_id, self.s.twitch_client_secret), logins))
        else:
            log.info("no twitch client id set: skipping live checks (chat still monitored)")
        log.info("watching %d channels", len(logins))
        await asyncio.gather(*tasks)


def main() -> None:
    p = argparse.ArgumentParser(description="Clipping watcher")
    p.add_argument("--root", default=".", help="folder containing settings.yaml / campaigns.yaml")
    p.add_argument("--channels", nargs="*", help="quick test: twitch logins to watch, ignores campaigns.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    s = config.load(args.root)
    if args.channels:
        s.campaigns = [config.Campaign("adhoc", True, [config.ChannelRef("twitch", c.lower()) for c in args.channels])]
    try:
        asyncio.run(Watcher(s).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
