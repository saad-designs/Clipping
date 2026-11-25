"""SQLite storage for detected moments. One file, no server to run."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS moments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,          -- e.g. twitch:xqc
    campaigns   TEXT NOT NULL,          -- JSON list of campaign names
    at          REAL NOT NULL,          -- unix time of the moment
    score       REAL NOT NULL,
    signals     TEXT NOT NULL,          -- JSON
    status      TEXT NOT NULL DEFAULT 'new',   -- new | approved | rejected | downloaded
    clip_path   TEXT,                   -- filled in step 2
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_status ON moments(status, at);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add_moment(self, channel: str, campaigns: list[str], at: float, score: float, signals: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO moments (channel, campaigns, at, score, signals, created) VALUES (?,?,?,?,?,?)",
            (channel, json.dumps(campaigns), at, score, json.dumps(signals), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM moments ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
