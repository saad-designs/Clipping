"""Hype scorer: turns a stream of chat messages into "moment" events.

Each channel is scored against its OWN recent history (z-scores), so a 50-viewer
stream and a 50k-viewer stream are judged fairly. Every bucket (default 5s) we measure:
  - messages: how many chat messages arrived
  - hype:     how many messages contained a hype token (KEKW, W, ????, CLIP...)
  - unique:   how many different people chatted
and compare each to the previous ~10 minutes. The weighted sum of the z-scores is the
hype score. A moment fires when it beats the threshold, then the channel cools down.

When the stream is being recorded, audio loudness is added as a 4th signal (a boost only).
"""
from __future__ import annotations

import math
import re
from collections import Counter
import statistics
from collections import deque
from dataclasses import dataclass, field

from shared.config import ScorerConfig
from shared.hype_tokens import is_hype, is_noise


@dataclass
class Moment:
    channel: str
    at: float            # estimated real time of the moment (chat lag already removed)
    score: float
    signals: dict        # raw counts + z-scores, kept so thresholds can be tuned later


def phrase(text: str) -> str:
    """Normalise a chat message for counting: 'KEKW KEKW kekw' -> 'KEKW', 'no way!!' -> 'NO WAY'."""
    t = re.sub(r"\s+", " ", re.sub(r"[!.,]+", " ", text.upper())).strip()
    words = t.split(" ")
    if words and all(w == words[0] for w in words):
        t = words[0]
    return t if 0 < len(t) <= 24 else ""


@dataclass
class _Bucket:
    start: float
    messages: int = 0
    hype: int = 0
    users: dict = field(default_factory=dict)   # user -> messages sent this bucket
    phrases: Counter = field(default_factory=Counter)   # what chat is spamming ("KEKW", "NO WAY")

    def values(self) -> dict[str, float]:
        return {"messages": self.messages, "hype": self.hype, "unique": len(self.users)}

    def add(self, user: str, hype: bool, per_user_cap: int, text: str = "") -> None:
        n = self.users.get(user, 0)
        self.users[user] = n + 1
        if n >= per_user_cap:   # one spammer can't fake a surge
            return
        self.messages += 1
        self.hype += hype
        p = phrase(text)
        if p:
            self.phrases[p] += 1


def _z(value: float, history: list[float], floor: float) -> float:
    n = len(history)
    mean = sum(history) / n
    var = sum((h - mean) ** 2 for h in history) / n
    # The std floor stops a dead-quiet chat (std≈0) from producing huge z-scores off 3 messages.
    return (value - mean) / max(math.sqrt(var), floor)


class ChannelScorer:
    """Chat buckets are scored `score_delay_seconds` after they end, so the audio for the
    same moment (which arrives only once a recorded segment finishes) can be included."""

    AUDIO_FLOOR = 3.0   # LU; quieter-than-this variation is just normal talking

    def __init__(self, channel: str, cfg: ScorerConfig, extra_tokens: set[str] | None = None,
                 collect: bool = False):
        self.channel = channel
        # VOD mode: keep every scored bucket (start, score, z-scores) to rank the best N afterwards
        self.collected: list[tuple[float, float, dict]] | None = [] if collect else None
        self.cfg = cfg
        self.extra = frozenset(extra_tokens or ())
        self.history: deque[dict[str, float]] = deque(maxlen=cfg.baseline_buckets)
        self.audio: deque[tuple[float, float]] = deque(maxlen=cfg.baseline_buckets * cfg.bucket_seconds + 120)
        self.audio_history: deque[float] = deque(maxlen=cfg.baseline_buckets)
        self.open: dict[float, _Bucket] = {}
        self.next_close: float | None = None
        self.last_fired = -math.inf
        self.fired: deque[tuple[float, float]] = deque()   # (time, score) of recent moments

    def _bucket_start(self, ts: float) -> float:
        b = self.cfg.bucket_seconds
        return ts - (ts % b)

    def add_message(self, ts: float, user: str, text: str) -> list[Moment]:
        fired = self.tick(ts)
        if is_noise(user, text):   # bots and !commands
            return fired
        start = self._bucket_start(ts)
        if self.next_close is None:
            self.next_close = start
        if start < self.next_close:   # late message for an already-scored bucket
            return fired
        bucket = self.open.setdefault(start, _Bucket(start))
        bucket.add(user, is_hype(text, self.extra), self.cfg.per_user_cap, text)
        return fired

    def add_audio(self, ts: float, lufs: float) -> None:
        """One loudness reading (peak LUFS for the second starting at `ts`)."""
        self.audio.append((ts, lufs))

    def tick(self, now: float) -> list[Moment]:
        """Score every bucket whose delay has passed. Call regularly, even when chat is silent."""
        fired: list[Moment] = []
        b, delay = self.cfg.bucket_seconds, self.cfg.score_delay_seconds
        while self.next_close is not None and now >= self.next_close + b + delay:
            # Missing buckets are empty ones: silence is part of the baseline.
            bucket = self.open.pop(self.next_close, None) or _Bucket(self.next_close)
            m = self._close(bucket)
            if m:
                fired.append(m)
            self.next_close += b
        return fired

    def reset(self) -> None:
        """Call when a stream goes offline so the next stream starts with a fresh baseline."""
        self.history.clear()
        self.audio.clear()
        self.audio_history.clear()
        self.open.clear()
        self.next_close = None

    def _beats_hourly_cap(self, now: float, score: float) -> bool:
        """Huge chats cross the threshold constantly. Past the hourly cap, a moment must beat the
        median of this hour's moments, so you review that streamer's best, not all of it."""
        while self.fired and self.fired[0][0] < now - 3600:
            self.fired.popleft()
        cap = self.cfg.max_moments_per_hour
        return not cap or len(self.fired) < cap or score > statistics.median(s for _, s in self.fired)

    def _audio_peak(self, bucket: _Bucket) -> float | None:
        # Chat reacts ~chat_lag seconds AFTER the moment, so look back that far for the sound.
        lo = bucket.start - self.cfg.chat_lag_seconds - self.cfg.bucket_seconds
        hi = bucket.start + self.cfg.bucket_seconds
        vals = [l for t, l in self.audio if lo <= t < hi]
        return max(vals) if vals else None

    def _close(self, bucket: _Bucket) -> Moment | None:
        vals = bucket.values()
        cfg = self.cfg
        moment = None
        peak = self._audio_peak(bucket)
        if len(self.history) >= cfg.warmup_buckets and vals["messages"] >= cfg.min_messages:
            floors = {"messages": 2.0, "hype": 1.0, "unique": 2.0}
            zs = {k: _z(vals[k], [h[k] for h in self.history], floors[k]) for k in vals}
            if peak is not None and len(self.audio_history) >= cfg.warmup_buckets:
                zs["audio"] = _z(peak, list(self.audio_history), self.AUDIO_FLOOR)
            score = sum(cfg.weights.get(k, 0) * max(z, 0) for k, z in zs.items())
            at = bucket.start - cfg.chat_lag_seconds
            if self.collected is not None:
                self.collected.append((at, score, zs))
            # Volume alone (raid, giveaway command, bot spam) isn't a moment: chat must REACT.
            # Audio only boosts the score; a loud game sound with no chat reaction never fires.
            if (zs["hype"] >= cfg.min_hype_z and score >= cfg.threshold
                    and bucket.start - self.last_fired >= cfg.cooldown_seconds and self._beats_hourly_cap(bucket.start, score)):
                self.last_fired = bucket.start
                self.fired.append((bucket.start, score))
                sig = {**vals, **{f"z_{k}": round(z, 2) for k, z in zs.items()}}
                if peak is not None:
                    sig["audio_lufs"] = round(peak, 1)
                sig["top_chat"] = [[p, n] for p, n in bucket.phrases.most_common(5) if n >= 2]
                moment = Moment(self.channel, at, round(score, 2), sig)
        self.history.append(vals)
        if peak is not None:
            self.audio_history.append(peak)
        # drop audio we'll never look at again
        while self.audio and self.audio[0][0] < bucket.start - cfg.chat_lag_seconds - 2 * cfg.bucket_seconds:
            self.audio.popleft()
        return moment
