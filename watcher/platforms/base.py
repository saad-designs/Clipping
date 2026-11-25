"""Common shape every streaming platform implements.

The watcher only talks to this interface, so adding a platform means one new file.
"""
from __future__ import annotations

from typing import Callable

# (platform, login, unix_ts, user, text)
OnMessage = Callable[[str, str, float, str, str], None]


class Platform:
    name = "base"

    def __init__(self, logins: list[str], on_message: OnMessage):
        self.logins = logins
        self.on_message = on_message
        self.chat_connected: set[str] = set()   # for /api/health

    async def run_chat(self) -> None:
        """Long-running: deliver chat messages to on_message forever (reconnecting itself)."""
        raise NotImplementedError

    async def live_status(self) -> dict[str, dict] | None:
        """{login: info} for channels live right now. None = this platform can't tell (no API keys)."""
        return None

    def on_live_change(self, login: str, info: dict | None) -> None:
        """Called when a channel goes live (info) or offline (None)."""

    def record_cmd(self, login: str, info: dict | None, quality: str) -> list[str]:
        """Command that writes the live stream as MPEG-TS to stdout."""
        raise NotImplementedError


def streamlink(url: str, quality: str, *extra: str) -> list[str]:
    return ["streamlink", "--stdout", "--loglevel", "error", *extra, url, quality]
