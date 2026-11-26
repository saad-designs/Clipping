"""Runs the stream watcher on this PC, managed by Clipping Studio ("one-PC mode").

Studio writes the watcher's settings.yaml / campaigns.yaml under studio_data/watcher/,
starts `python -m watcher.app` in the background, restarts it if it stops (or when it asks
to, after campaigns change), and stops it when Studio closes. No server, SSH or tokens to set up.
"""
from __future__ import annotations

import logging
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

log = logging.getLogger("local_watcher")

REPO = Path(__file__).resolve().parent.parent
PORT = 8787
RESTART_EXIT_CODE = 3            # watcher/app.py exits with this to be relaunched
QUALITY = {"720p": "720p60,720p,best", "1080p": "best", "480p": "480p,720p,best"}
MBPS = {"720p": 3.0, "1080p": 6.0, "480p": 1.5}


class LocalWatcher:
    def __init__(self, store, python: str | None = None):
        self.store = store
        self.root = store.root / "watcher"
        self.root.mkdir(parents=True, exist_ok=True)
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.started_at: float | None = None
        self._stop = threading.Event()
        self._kick = threading.Event()      # set by restart(): re-check campaigns
        self.waiting = False                # no streamers in any active campaign yet
        self._requested = False             # we stopped it on purpose (restart): relaunch at once
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- config files ---------------------------------------------------------
    def token(self) -> str:
        s = self.store.settings()
        if not s.get("local_token"):
            self.store.save_settings({"local_token": secrets.token_urlsafe(24)})
        return self.store.settings()["local_token"]

    def write_settings(self) -> Path:
        s = self.store.settings()
        q = s.get("quality") or "720p"
        data = {
            "data_dir": "data",
            "twitch": {"client_id": s.get("twitch_client_id") or "", "client_secret": s.get("twitch_client_secret") or ""},
            "recorder": {"quality": QUALITY.get(q, q), "max_concurrent": int(s.get("max_recording") or 3),
                         "min_free_gb": 10},
            "api": {"host": "127.0.0.1", "port": PORT, "token": self.token()},
        }
        path = self.root / "settings.yaml"
        path.write_text("# Written by Clipping Studio (Settings page). Changes here get overwritten.\n"
                        + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    def write_campaigns(self) -> Path:
        from shared import config
        return config.write_campaigns(self.root, campaign_payload(self.store.campaigns()))

    # ---- process supervision ----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.write_settings()
        self.write_campaigns()
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="local-watcher", daemon=True)
        self._thread.start()

    def _launch(self) -> subprocess.Popen:
        logf = open(self.root / "watcher.log", "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        return subprocess.Popen([self.python, "-m", "watcher.app", "--root", str(self.root)], cwd=str(REPO),
                                stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)

    def _supervise(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            if not any(c["active"] and c["channels"] for c in self.store.campaigns()):
                self.waiting = True          # nothing to watch: wait for a campaign instead of crash-looping
                self._kick.wait(30)
                self._kick.clear()
                continue
            self.waiting = False
            with self._lock:
                self.proc = self._launch()
                self.started_at = time.time()
            log.info("stream watcher started (pid %s)", self.proc.pid)
            code = self.proc.wait()
            self.last_exit = code
            if self._stop.is_set():
                break
            self.restarts += 1
            ran = time.time() - (self.started_at or time.time())
            if self._requested or code == RESTART_EXIT_CODE or code == 0 and ran < 5:
                self._requested = False
                wait = 1
            else:   # crashed: back off so a broken setup doesn't spin
                backoff = 2 if ran > 120 else min(backoff * 2, 60)
                wait = backoff
                log.warning("stream watcher stopped (exit %s); restarting in %ss", code, wait)
            self._stop.wait(wait)

    def restart(self) -> None:
        """Re-read settings/campaigns: write the files, stop the process; the supervisor relaunches it."""
        self.write_settings()
        self.write_campaigns()
        self._kick.set()
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self._requested = True
                self.proc.terminate()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def status(self) -> dict:
        s = self.store.settings()
        q = s.get("quality") or "720p"
        n = int(s.get("max_recording") or 3)
        return {"running": self.running(), "waiting": self.waiting, "pid": self.proc.pid if self.proc else None, "restarts": self.restarts,
                "last_exit": self.last_exit, "uptime_s": int(time.time() - self.started_at) if self.running() and self.started_at else 0,
                "max_gb_per_hour": round(MBPS.get(q, 3.0) * n * 0.45, 1), "log": self.log_tail()}

    def log_tail(self, lines: int = 40) -> str:
        f = self.root / "watcher.log"
        if not f.exists():
            return ""
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 16000))
            text = fh.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])


def campaign_payload(campaigns: list[dict]) -> list[dict]:
    return [{"name": c["name"], "active": c["active"], "min_seconds": c["min_seconds"], "max_seconds": c["max_seconds"],
             "required_text": c["required_text"], "extra_hype_tokens": c.get("extra_hype_tokens") or [],
             "channels": [{"platform": ch["platform"], "login": ch["login"]} for ch in c["channels"]]}
            for c in campaigns]


def keep_awake(on: bool = True) -> bool:
    """Ask Windows not to sleep while Studio runs (the screen can still turn off). No-op elsewhere."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        return True
    except Exception as e:
        log.warning("couldn't stop Windows from sleeping: %s", e)
        return False
