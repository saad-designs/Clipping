"""Login for the dashboard, so it can be reached from the internet (Tailscale Funnel / Vercel).

- Every /api request needs `Authorization: Bearer <token>`. Tokens come from logging in with
  the dashboard password (POST /api/login), or from the one-time link Studio opens on this PC
  at start-up (so the PC's own browser never asks).
- Videos, logos and frames are loaded by <video>/<img> tags, which can't send headers: those
  URLs carry a signature instead (`?sig=`), tied to that exact path and valid for a few hours.
- Too many wrong passwords lock the login for a while.
- No password set yet: only the start-up link works; remote login is refused until one is set.
- CORS is open: auth is a bearer token, never a cookie, so another site can't act as you.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

TOKEN_DAYS = 30
MEDIA_HOURS = 6
MAX_FAILS, LOCK_MINUTES = 5, 10
PUBLIC = {"/api/login", "/api/ping"}
SIGNED_PREFIXES = ("/api/clips/", "/api/campaigns/")       # media under these accept ?sig=
SIGNED_SUFFIXES = ("/video", "/asset/logo", "/frame")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, label TEXT NOT NULL, created REAL NOT NULL,
                                     expires REAL NOT NULL, last_used REAL NOT NULL);
"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(pw, salt), stored)


class Auth:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()
        self.fails: list[float] = []

    # ---- secrets kept in the settings table (not in SETTING_DEFAULTS: never sent to the page)
    def _get(self, key: str) -> str:
        with self.store.lock:
            row = self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        import json
        return json.loads(row["value"]) if row else ""

    def _set(self, key: str, value: str) -> None:
        import json
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
            self.store.db.commit()

    @property
    def media_secret(self) -> bytes:
        s = self._get("auth_media_secret")
        if not s:
            s = secrets.token_hex(32)
            self._set("auth_media_secret", s)
        return bytes.fromhex(s)

    def password_set(self) -> bool:
        return bool(self._get("auth_password_hash"))

    def set_password(self, pw: str) -> None:
        if len(pw) < 10:
            raise ValueError("use at least 10 characters")
        self._set("auth_password_hash", hash_password(pw))

    # ---- sessions
    def new_token(self, label: str, days: float = TOKEN_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.store.lock:
            self.store.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (_h(token), label, now, now + days * 86400, now))
            self.store.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self.store.db.commit()
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        with self.store.lock:
            row = self.store.db.execute("SELECT expires FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
            if not row or row["expires"] < now:
                return False
            self.store.db.execute("UPDATE sessions SET last_used=? WHERE token_hash=?", (now, _h(token)))
            self.store.db.commit()
        return True

    def revoke(self, token: str) -> None:
        with self.store.lock:
            self.store.db.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))
            self.store.db.commit()

    def revoke_all(self) -> None:
        with self.store.lock:
            self.store.db.execute("DELETE FROM sessions")
            self.store.db.commit()

    def login(self, pw: str) -> str:
        """A token for the right password. Raises PermissionError (wrong / locked / none set)."""
        now = time.time()
        self.fails = [t for t in self.fails if now - t < LOCK_MINUTES * 60]
        if len(self.fails) >= MAX_FAILS:
            raise PermissionError(f"too many wrong passwords: try again in {LOCK_MINUTES} minutes")
        stored = self._get("auth_password_hash")
        if not stored:
            raise PermissionError("no dashboard password yet: set one in Settings on your PC first")
        if not check_password(pw, stored):
            self.fails.append(now)
            raise PermissionError("wrong password")
        self.fails.clear()
        return self.new_token("login")

    # ---- signed media URLs
    def sign(self, path: str, channel: str | None = None, hours: float = MEDIA_HOURS) -> str:
        """A URL for `path` that works without a login for a few hours (for <video>/<img>)."""
        from urllib.parse import quote
        exp = int(time.time() + hours * 3600)
        signed = path + (f"?channel={channel}" if channel else "")
        mac = hmac.new(self.media_secret, f"{signed}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return path + (f"?channel={quote(channel, safe='')}&" if channel else "?") + f"sig={exp}.{mac}"

    def signed_ok(self, path: str, sig: str) -> bool:
        try:
            exp_s, mac = sig.split(".", 1)
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < time.time():
            return False
        good = hmac.new(self.media_secret, f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(good, mac)

    # ---- the request check
    def allowed(self, path: str, query: dict, headers: dict) -> bool:
        if not path.startswith("/api/") or path in PUBLIC:
            return True
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and self.valid(auth[7:].strip()):
            return True
        sig = query.get("sig", "")
        if sig and path.startswith(SIGNED_PREFIXES) and path.endswith(SIGNED_SUFFIXES):
            # the channel picked for a frame is part of what's signed
            signed_path = path + (f"?channel={query['channel']}" if "channel" in query else "")
            return self.signed_ok(signed_path, sig)
        return False
