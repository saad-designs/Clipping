"""Hype scorer: turns a stream of chat messages into "moment" events.

Each channel is scored against its OWN recent history (z-scores), so a 50-viewer
stream and a 50k-viewer stream are judged fairly. Every bucket (default 5s) we measure:
  - messages: how many chat messages arrived
  - hype:     how many messages contained a hype token (KEKW, W, ????, CLIP...)
  - unique:   how many different people chatted
and compare each to the previous ~10 minutes. The weighted sum of the z-scores is the
hype score. A moment fires when it beats the threshold, then the channel cools down.

Step 2 will add an audio-loudness signal through `add_signal` with the same logic.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from shared.config import ScorerConfig
from shared.hype_tokens import is_hype


@dataclass
class Moment:
    channel: str
    at: float            # estimated real time of the moment (chat lag already removed)
    score: float
    signals: dict        # raw counts + z-scores, kept so thresholds can be tuned later


@dataclass
class _Bucket:
    start: float
    messages: int = 0
    hype: int = 0
    users: dict = field(default_factory=dict)   # user -> messages sent this bucket

    def values(self) -> dict[str, float]:
        return {"messages": self.messages, "hype": self.hype, "unique": len(self.users)}

    def add(self, user: str, hype: bool, per_user_cap: int) -> None:
        n = self.users.get(user, 0)
        self.users[user] = n + 1
        if n >= per_user_cap:   # one spammer can't fake a surge
            return
        self.messages += 1
        self.hype += hype


def _z(value: float, history: list[float], floor: float) -> float:
    n = len(history)
    mean = sum(history) / n
    var = sum((h - mean) ** 2 for h in history) / n
    # The std floor stops a dead-quiet chat (std≈0) from producing huge z-scores off 3 messages.
    return (value - mean) / max(math.sqrt(var), floor)


class ChannelScorer:
    def __init__(self, channel: str, cfg: ScorerConfig, extra_tokens: set[str] | None = None):
        self.channel = channel
        self.cfg = cfg
        self.extra = frozenset(extra_tokens or ())
        self.history: deque[dict[str, float]] = deque(maxlen=cfg.baseline_buckets)
        self.current: _Bucket | None = None
        self.last_fired = -math.inf

    def _bucket_start(self, ts: float) -> float:
        b = self.cfg.bucket_seconds
        return ts - (ts % b)

    def add_message(self, ts: float, user: str, text: str) -> list[Moment]:
        fired = self.tick(ts)
        if self.current is None:
            self.current = _Bucket(self._bucket_start(ts))
        self.current.add(user, is_hype(text, self.extra), self.cfg.per_user_cap)
        return fired

    def tick(self, now: float) -> list[Moment]:
        """Close any finished buckets. Call this regularly even when chat is silent."""
        fired: list[Moment] = []
        b = self.cfg.bucket_seconds
        while self.current is not None and now >= self.current.start + b:
            m = self._close(self.current)
            if m:
                fired.append(m)
            # Keep rolling forward with empty buckets: silence is part of the baseline.
            self.current = _Bucket(self.current.start + b)
        return fired

    def reset(self) -> None:
        """Call when a stream goes offline so the next stream starts with a fresh baseline."""
        self.history.clear()
        self.current = None

    def _close(self, bucket: _Bucket) -> Moment | None:
        vals = bucket.values()
        cfg = self.cfg
        moment = None
        if len(self.history) >= cfg.warmup_buckets and vals["messages"] >= cfg.min_messages:
            floors = {"messages": 2.0, "hype": 1.0, "unique": 2.0}
            zs = {k: _z(vals[k], [h[k] for h in self.history], floors[k]) for k in vals}
            score = sum(cfg.weights.get(k, 0) * max(z, 0) for k, z in zs.items())
            at = bucket.start - cfg.chat_lag_seconds
            # Volume alone (raid, giveaway command, bot spam) isn't a moment: chat must REACT.
            if zs["hype"] >= cfg.min_hype_z and score >= cfg.threshold and bucket.start - self.last_fired >= cfg.cooldown_seconds:
                self.last_fired = bucket.start
                moment = Moment(self.channel, at, round(score, 2),
                                {**vals, **{f"z_{k}": round(z, 2) for k, z in zs.items()}})
        self.history.append(vals)
        return moment
