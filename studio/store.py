"""Local state for the PC dashboard: settings, campaigns (+ their files) and clips."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

SETTING_DEFAULTS = {
    "server_url": "", "token": "", "anthropic_api_key": "", "model": "claude-opus-5-5", "effort": "medium",
    "director": "free", "gemini_api_key": "", "encoder": "auto", "whisper_model": "small", "language": None, "poll_minutes": 3,
    "output_dir": "Campaigns",
    # one-PC mode: the stream watcher runs on this PC, started by Studio
    "find_mode": "vods",                 # vods: clip finished streams (free) | local: live on this PC | server
    "sweep_hours": 2, "sweep_days": 3,   # vods mode: check every N hours; look back N days the first time
    "local_watcher": False, "local_token": "", "twitch_client_id": "", "twitch_client_secret": "",
    "max_recording": 3, "quality": "720p",
}

CAMPAIGN_DEFAULTS = {
    "name": "", "url": "", "brief": "", "active": True, "channels": [],     # [{platform, login, facecam}]
    "min_seconds": 15, "max_seconds": 60, "target_seconds": 32, "required_text": [], "cta": "", "banned": "",
    "layout": None, "caption_style": None, "hook": True, "brand_color": "#E8202A",
    "clips_per_stream": 5,
    "pronoun": "they",                   # streamer's pronoun in hooks: she | he | they
    "games": [],                         # only clip these games (Twitch category / stream title); [] = any
    "logo_path": None, "logo_corner": None, "logo_scale": 0.16, "logo_opacity": 0.92,
    "intro_path": None, "outro_path": None, "font_path": None, "font_name": None, "extra_hype_tokens": [],
}

CLIP_STATUSES = ("queued", "editing", "review", "approved", "rejected", "posted", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'live',      -- live | vod | upload
    moment_id INTEGER,                        -- the server's moment id (live captures)
    src_path TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',          -- moment_offset, signals, score, ...
    status TEXT NOT NULL DEFAULT 'queued',
    plan TEXT,                                -- the director's validated plan
    lock_plan INTEGER NOT NULL DEFAULT 0,     -- 1 = re-render this exact plan (text edits), skip the director
    notes TEXT NOT NULL DEFAULT '',           -- re-edit instructions from the user
    out_path TEXT,
    words TEXT,                               -- cached transcript
    facecam TEXT,                             -- cached webcam detection
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status, updated);
CREATE TABLE IF NOT EXISTS seen_vods (key TEXT PRIMARY KEY, at REAL NOT NULL);       -- streams already clipped
CREATE TABLE IF NOT EXISTS sweeps (channel TEXT PRIMARY KEY, at REAL NOT NULL);      -- last check per streamer
CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_moment ON clips(moment_id, campaign) WHERE moment_id IS NOT NULL;
"""


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "campaign"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "campaigns").mkdir(exist_ok=True)
        (self.root / "clips").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "studio.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- settings -----------------------------------------------------------
    def settings(self) -> dict:
        with self.lock:
            rows = {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}
        s = {**SETTING_DEFAULTS, **rows}
        s["local_watcher"] = s.get("find_mode") == "local"
        if s.get("local_watcher"):      # the watcher runs on this PC: talk to it directly
            s["server_url"], s["token"] = "http://127.0.0.1:8787", s.get("local_token", "")
        return s

    def save_settings(self, values: dict) -> dict:
        with self.lock:
            for k, v in values.items():
                if k in SETTING_DEFAULTS:
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(v)))
            self.db.commit()
        return self.settings()

    def output_dir(self) -> Path:
        out = Path(self.settings()["output_dir"]).expanduser()
        return out if out.is_absolute() else self.root.parent / out

    # ---- campaigns ----------------------------------------------------------
    def campaign_dir(self, slug: str) -> Path:
        return self.root / "campaigns" / slug

    def campaigns(self) -> list[dict]:
        out = []
        for f in sorted((self.root / "campaigns").glob("*/campaign.json")):
            out.append({**CAMPAIGN_DEFAULTS, **json.loads(f.read_text(encoding="utf-8"))})
        return out

    def campaign(self, slug_or_name: str) -> dict | None:
        for c in self.campaigns():
            if c["slug"] == slug_or_name or c["name"] == slug_or_name:
                return c
        return None

    def save_campaign(self, data: dict) -> dict:
        with self.lock:
            existing = self.campaign(data.get("slug") or "") or {}
            c = {**CAMPAIGN_DEFAULTS, **existing, **data}
            c["name"] = (c["name"] or "").strip()
            if not c["name"]:
                raise ValueError("campaign needs a name")
            c["slug"] = existing.get("slug") or slugify(c["name"])
            c["required_text"] = [t.strip() for t in c.get("required_text") or [] if t and t.strip()]
            chans = []
            for ch in c.get("channels") or []:
                login = str(ch.get("login", "")).strip().lstrip("@")
                if login:
                    plat = (ch.get("platform") or "twitch").lower()
                    chans.append({"platform": plat, "login": login if plat == "youtube" else login.lower(),
                                  "facecam": ch.get("facecam") or "auto"})   # auto | none | [x,y,w,h]
            c["channels"] = chans
            d = self.campaign_dir(c["slug"])
            d.mkdir(parents=True, exist_ok=True)
            (d / "campaign.json").write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
            return c

    def save_asset(self, slug: str, kind: str, filename: str, data: bytes) -> dict:
        c = self.campaign(slug)
        if not c:
            raise KeyError(slug)
        ext = Path(filename).suffix.lower() or ".bin"
        path = self.campaign_dir(slug) / f"{kind}{ext}"
        for old in self.campaign_dir(slug).glob(f"{kind}.*"):
            old.unlink(missing_ok=True)
        path.write_bytes(data)
        c[f"{kind}_path"] = str(path)
        if kind == "font":
            c["font_name"] = _font_family(path) or c.get("font_name")
        return self.save_campaign(c)

    def delete_campaign(self, slug: str) -> None:
        shutil.rmtree(self.campaign_dir(slug), ignore_errors=True)

    # ---- clips --------------------------------------------------------------
    def add_clip(self, campaign: str, src_path: str, channel: str = "", source: str = "live",
                 moment_id: int | None = None, meta: dict | None = None) -> int | None:
        now = time.time()
        with self.lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO clips (campaign, channel, source, moment_id, src_path, meta, created, updated) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (campaign, channel, source, moment_id, src_path, json.dumps(meta or {}), now, now))
            except sqlite3.IntegrityError:      # this moment is already in this campaign
                return None
            self.db.commit()
            return cur.lastrowid

    def clip(self, cid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        return _clip_dict(r) if r else None

    def clips(self, status: str | None = None, limit: int = 300) -> list[dict]:
        q, args = "SELECT * FROM clips", []
        if status:
            statuses = status.split(",")
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args += statuses
        q += " ORDER BY updated DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [_clip_dict(r) for r in self.db.execute(q, args).fetchall()]

    def update_clip(self, cid: int, **fields) -> None:
        if not fields:
            return
        for k in ("meta", "plan", "words", "facecam"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), cid))
            self.db.commit()

    def next_queued(self) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM clips WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        return _clip_dict(r) if r else None

    def recent_styles(self, campaign: str, n: int = 4) -> list[str]:
        with self.lock:
            rows = self.db.execute("SELECT plan FROM clips WHERE campaign=? AND plan IS NOT NULL "
                                   "ORDER BY updated DESC LIMIT ?", (campaign, n)).fetchall()
        return [json.loads(r["plan"]).get("caption_style") for r in rows][::-1]

    def recent_hooks(self, campaign: str, n: int = 6) -> list[str]:
        with self.lock:
            rows = self.db.execute("SELECT plan FROM clips WHERE campaign=? AND plan IS NOT NULL "
                                   "ORDER BY updated DESC LIMIT ?", (campaign, n)).fetchall()
        return [((json.loads(r["plan"]).get("hook") or {}).get("text") or "") for r in rows]

    def seen_vods(self) -> list[str]:
        with self.lock:
            return [r["key"] for r in self.db.execute("SELECT key FROM seen_vods")]

    def mark_vod(self, key: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO seen_vods VALUES (?, ?)", (key, time.time()))
            self.db.commit()

    def last_sweep(self, channel: str) -> float:
        with self.lock:
            r = self.db.execute("SELECT at FROM sweeps WHERE channel=?", (channel,)).fetchone()
        return r["at"] if r else 0.0

    def set_last_sweep(self, channel: str, at: float) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO sweeps VALUES (?, ?)", (channel, at))
            self.db.commit()

    def counts(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) n FROM clips GROUP BY status").fetchall()
        return {s: 0 for s in CLIP_STATUSES} | {r["status"]: r["n"] for r in rows}

    def reset_stuck(self) -> None:
        """Clips left 'editing' by a crash go back to the queue on start."""
        with self.lock:
            self.db.execute("UPDATE clips SET status='queued' WHERE status='editing'")
            self.db.commit()


def _clip_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("meta", "plan", "words", "facecam"):
        d[k] = json.loads(d[k]) if d.get(k) else ({} if k == "meta" else None)
    return d


def _font_family(path: Path) -> str | None:
    """Family name from a .ttf/.otf so captions can use an uploaded brand font."""
    try:
        from fontTools.ttLib import TTFont   # optional dependency
        names = TTFont(str(path))["name"]
        return str(names.getDebugName(1))
    except Exception:
        return path.stem.replace("-", " ").replace("_", " ")
