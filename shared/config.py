"""Loads settings.yaml and campaigns.yaml. Missing files fall back to defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ScorerConfig:
    bucket_seconds: int = 5
    baseline_buckets: int = 120
    warmup_buckets: int = 24
    threshold: float = 6.0
    cooldown_seconds: int = 60
    min_messages: int = 8
    chat_lag_seconds: int = 8
    min_hype_z: float = 2.0
    per_user_cap: int = 2
    score_delay_seconds: int = 15
    weights: dict = field(default_factory=lambda: {"messages": 1.0, "hype": 1.5, "unique": 1.0, "audio": 1.0})


@dataclass
class RecorderConfig:
    quality: str = "best"
    segment_seconds: int = 10
    buffer_minutes: int = 10
    pre_roll: int = 35
    post_roll: int = 20
    clip_retention_days: int = 3
    max_concurrent: int = 20


@dataclass
class ChannelRef:
    platform: str
    login: str

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.login}"


@dataclass
class Campaign:
    name: str
    active: bool
    channels: list[ChannelRef]
    min_seconds: int = 15
    max_seconds: int = 60
    extra_hype_tokens: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Settings:
    data_dir: Path
    twitch_client_id: str
    twitch_client_secret: str
    scorer: ScorerConfig
    recorder: RecorderConfig
    campaigns: list[Campaign]

    def active_channels(self) -> dict[str, tuple[ChannelRef, list[Campaign]]]:
        """channel key -> (channel, campaigns that include it). One chat connection per channel."""
        out: dict[str, tuple[ChannelRef, list[Campaign]]] = {}
        for c in self.campaigns:
            if not c.active:
                continue
            for ch in c.channels:
                out.setdefault(ch.key, (ch, []))[1].append(c)
        return out


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load(root: Path | str = ".") -> Settings:
    root = Path(root)
    s = _read(root / "settings.yaml")
    c = _read(root / "campaigns.yaml")

    sc = s.get("scorer", {}) or {}
    scorer = ScorerConfig(**{k: v for k, v in sc.items() if k in ScorerConfig.__dataclass_fields__ and k != "weights"})
    scorer.weights.update(sc.get("weights", {}) or {})   # partial weights keep the other defaults
    rc = s.get("recorder", {}) or {}
    recorder = RecorderConfig(**{k: v for k, v in rc.items() if k in RecorderConfig.__dataclass_fields__})

    campaigns = []
    for raw in c.get("campaigns", []) or []:
        clip = raw.get("clip", {}) or {}
        campaigns.append(Campaign(
            name=raw["name"],
            active=raw.get("active", True),
            channels=[ChannelRef(ch["platform"].lower(), str(ch["login"]).lower())
                      for ch in raw.get("channels", [])],
            min_seconds=clip.get("min_seconds", 15),
            max_seconds=clip.get("max_seconds", 60),
            extra_hype_tokens=[t.upper() for t in raw.get("extra_hype_tokens", [])],
            notes=raw.get("notes", ""),
        ))

    tw = s.get("twitch", {}) or {}
    return Settings(
        data_dir=root / s.get("data_dir", "data"),
        twitch_client_id=tw.get("client_id", "") or "",
        twitch_client_secret=tw.get("client_secret", "") or "",
        scorer=scorer,
        recorder=recorder,
        campaigns=campaigns,
    )
