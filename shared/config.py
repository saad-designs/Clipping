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
    max_moments_per_hour: int = 6   # per channel; past this a moment must beat the hour's median score
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
    min_free_gb: float = 15        # below this: no new recordings, oldest clips pruned


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"          # reach it over Tailscale; don't open this port to the internet
    port: int = 8787
    token: str = ""                # required; every request must send it


@dataclass
class KickConfig:
    # Kick's public Pusher key for chat. Kick changes it occasionally; update here if chat stops.
    pusher_key: str = "32cbd69e4b950bf97679"
    pusher_cluster: str = "us2"


@dataclass
class YouTubeConfig:
    # Netscape cookies.txt exported from a logged-in browser; fixes "confirm you're not a bot".
    cookies_file: str = ""


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
    required_text: list[str] = field(default_factory=list)   # hashtags/mentions the campaign demands
    layout: str | None = None      # blur | crop | facecam (Studio default if empty)

    def public(self) -> dict:
        """What Studio needs to know about this campaign."""
        return {"name": self.name, "active": self.active, "min_seconds": self.min_seconds,
                "max_seconds": self.max_seconds, "required_text": self.required_text,
                "layout": self.layout, "notes": self.notes,
                "channels": [c.key for c in self.channels]}


@dataclass
class Settings:
    data_dir: Path
    twitch_client_id: str
    twitch_client_secret: str
    scorer: ScorerConfig
    recorder: RecorderConfig
    campaigns: list[Campaign]
    api: ApiConfig = field(default_factory=ApiConfig)
    kick: KickConfig = field(default_factory=KickConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)

    def campaign(self, name: str) -> Campaign | None:
        return next((c for c in self.campaigns if c.name == name), None)

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


def _channel(raw: dict) -> ChannelRef:
    platform = raw["platform"].lower()
    login = str(raw["login"]).strip().lstrip("@")
    # YouTube channel ids (UC...) are case-sensitive; Twitch/Kick names are not.
    if not (platform == "youtube" and login.startswith("UC")):
        login = login.lower()
    return ChannelRef(platform, login)


def _make(cls, raw: dict | None):
    raw = raw or {}
    return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


def load(root: Path | str = ".") -> Settings:
    root = Path(root)
    s = _read(root / "settings.yaml")
    c = _read(root / "campaigns.yaml")

    sc = s.get("scorer", {}) or {}
    scorer = ScorerConfig(**{k: v for k, v in sc.items() if k in ScorerConfig.__dataclass_fields__ and k != "weights"})
    scorer.weights.update(sc.get("weights", {}) or {})   # partial weights keep the other defaults
    recorder = _make(RecorderConfig, s.get("recorder"))

    campaigns = []
    for raw in c.get("campaigns", []) or []:
        clip = raw.get("clip", {}) or {}
        campaigns.append(Campaign(
            name=raw["name"],
            active=raw.get("active", True),
            channels=[_channel(ch) for ch in raw.get("channels", [])],
            min_seconds=clip.get("min_seconds", 15),
            max_seconds=clip.get("max_seconds", 60),
            extra_hype_tokens=[t.upper() for t in raw.get("extra_hype_tokens", [])],
            notes=raw.get("notes", ""),
            required_text=list(raw.get("required_text", []) or []),
            layout=raw.get("layout") or None,
        ))

    tw = s.get("twitch", {}) or {}
    return Settings(
        data_dir=root / s.get("data_dir", "data"),
        twitch_client_id=tw.get("client_id", "") or "",
        twitch_client_secret=tw.get("client_secret", "") or "",
        scorer=scorer,
        recorder=recorder,
        campaigns=campaigns,
        api=_make(ApiConfig, s.get("api")),
        kick=_make(KickConfig, s.get("kick")),
        youtube=_make(YouTubeConfig, s.get("youtube")),
    )
