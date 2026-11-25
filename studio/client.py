"""Talks to the watcher's API. Standard library only."""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path


class ServerError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        h = {"X-Token": self.token, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ServerError("server rejected the token: check `token` in studio.yaml") from None
            raise ServerError(f"{method} {path} failed: {e.code} {e.read()[:200]!r}") from None
        except urllib.error.URLError as e:
            raise ServerError(f"can't reach {self.base} ({e.reason}). Is Tailscale on and the watcher running?") from None

    def get(self, path: str):
        with self._req("GET", path) as r:
            return json.load(r)

    def post(self, path: str, body: dict | None = None):
        with self._req("POST", path, body or {}) as r:
            return json.load(r)

    def download(self, path: str, dest: Path) -> Path:
        """Download to dest, resuming a partial .part file if one exists."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self._req("GET", path, headers=headers) as r:
            mode = "ab" if have and r.status == 206 else "wb"
            with open(part, mode) as f:
                shutil.copyfileobj(r, f, 1 << 20)
        part.replace(dest)
        return dest
