"""SQLite storage for detected moments. One file, no server to run."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STATUSES = ("new", "approved", "rejected", "downloaded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS moments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,          -- e.g. twitch:xqc
    campaigns   TEXT NOT NULL,          -- JSON list of campaign names
    at          REAL NOT NULL,          -- unix time of the moment
    score       REAL NOT NULL,
    signals     TEXT NOT NULL,          -- JSON
    status      TEXT NOT NULL DEFAULT 'new',   -- new | approved | rejected | downloaded
    clip_path   TEXT,                   -- mp4 cut from the buffer
    clip_start  REAL,                   -- unix time of the clip's first frame
    viewer_clips INTEGER NOT NULL DEFAULT 0,  -- Twitch viewers who clipped this moment
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_status ON moments(status, at);
"""

_MIGRATIONS = {  # columns added after step 1; older DBs get them on startup
    "clip_start": "ALTER TABLE moments ADD COLUMN clip_start REAL",
    "viewer_clips": "ALTER TABLE moments ADD COLUMN viewer_clips INTEGER NOT NULL DEFAULT 0",
}


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["campaigns"] = json.loads(d["campaigns"])
    d["signals"] = json.loads(d["signals"])
    return d


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The API thread pool and the main loop share this connection.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(moments)")}
        for col, sql in _MIGRATIONS.items():
            if col not in cols:
                self.conn.execute(sql)
        self.conn.commit()

    def add_moment(self, channel: str, campaigns: list[str], at: float, score: float, signals: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO moments (channel, campaigns, at, score, signals, created) VALUES (?,?,?,?,?,?)",
            (channel, json.dumps(campaigns), at, score, json.dumps(signals), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM moments ORDER BY at DESC LIMIT ?", (limit,)).fetchall()

    def list(self, status: str | None = None, limit: int = 200, has_clip: bool = False) -> list[dict]:
        q, args = "SELECT * FROM moments WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if has_clip:
            q += " AND clip_path IS NOT NULL"
        q += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        return [row_to_dict(r) for r in self.conn.execute(q, args).fetchall()]

    def get(self, moment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()

    def set_clip(self, moment_id: int, path: str, clip_start: float) -> None:
        self.conn.execute("UPDATE moments SET clip_path=?, clip_start=? WHERE id=?", (path, clip_start, moment_id))
        self.conn.commit()

    def update(self, moment_id: int, status: str | None = None, campaigns: list[str] | None = None) -> None:
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"bad status {status}")
            self.conn.execute("UPDATE moments SET status=? WHERE id=?", (status, moment_id))
        if campaigns is not None:
            self.conn.execute("UPDATE moments SET campaigns=? WHERE id=?", (json.dumps(campaigns), moment_id))
        self.conn.commit()

    def add_viewer_clips(self, channel: str, around: float, window: float, count: int = 1) -> int:
        """Credit viewer clips to the moment closest to `around` (within ±window). Returns its id or 0."""
        r = self.conn.execute(
            "SELECT id FROM moments WHERE channel=? AND at BETWEEN ? AND ? ORDER BY ABS(at-?) LIMIT 1",
            (channel, around - window, around + window, around)).fetchone()
        if not r:
            return 0
        self.conn.execute("UPDATE moments SET viewer_clips=viewer_clips+? WHERE id=?", (count, r["id"]))
        self.conn.commit()
        return r["id"]

    def counts_since(self, since: float) -> dict[str, int]:
        rows = self.conn.execute("SELECT channel, COUNT(*) n FROM moments WHERE at>=? GROUP BY channel", (since,))
        return {r["channel"]: r["n"] for r in rows}

    def prunable_clips(self, limit: int) -> list[sqlite3.Row]:
        """Clips to delete first when disk is low: rejected, then already downloaded, then unreviewed,
        approved-but-not-downloaded last (that's work you still want)."""
        return self.conn.execute(
            "SELECT id, clip_path FROM moments WHERE clip_path IS NOT NULL ORDER BY "
            "CASE status WHEN 'rejected' THEN 0 WHEN 'downloaded' THEN 1 WHEN 'new' THEN 2 ELSE 3 END, at ASC "
            "LIMIT ?", (limit,)).fetchall()

    def clear_clip(self, moment_id: int) -> None:
        self.conn.execute("UPDATE moments SET clip_path=NULL WHERE id=?", (moment_id,))
        self.conn.commit()
