"""Twitch: anonymous chat over IRC plus optional live-status checks via Helix.

Chat needs no account: Twitch allows read-only anonymous logins (justinfanNNNN).
One connection can join many channels, so monitoring 100 chats is cheap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
import urllib.request
from typing import Awaitable, Callable

log = logging.getLogger("twitch")

OnMessage = Callable[[str, float, str, str], Awaitable[None] | None]  # (login, ts, user, text)


def parse_privmsg(line: str) -> tuple[str, str, str] | None:
    """Returns (channel, user, text) for a chat line, else None.

    Example: :nick!nick@nick.tmi.twitch.tv PRIVMSG #xqc :KEKW
    """
    if line.startswith("@"):  # IRCv3 tags (only if requested); strip them
        line = line.split(" ", 1)[1]
    parts = line.split(" ", 3)
    if len(parts) < 4 or parts[1] != "PRIVMSG":
        return None
    user = parts[0][1:].split("!", 1)[0]
    channel = parts[2].lstrip("#")
    text = parts[3][1:] if parts[3].startswith(":") else parts[3]
    return channel, user, text


class TwitchChat:
    HOST, PORT = "irc.chat.twitch.tv", 6697

    def __init__(self, logins: list[str], on_message: OnMessage):
        self.logins = logins
        self.on_message = on_message

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                log.warning("chat disconnected (%s); retrying in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(self.HOST, self.PORT, ssl=True)
        nick = f"justinfan{random.randint(10000, 99999)}"
        writer.write(f"NICK {nick}\r\n".encode())
        # Twitch rate-limits JOINs (~20 per 10s for anonymous users); pace them.
        for i, login in enumerate(self.logins):
            writer.write(f"JOIN #{login}\r\n".encode())
            if i % 15 == 14:
                await writer.drain()
                await asyncio.sleep(11)
        await writer.drain()
        log.info("joined %d twitch chats", len(self.logins))
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    raise ConnectionError("server closed connection")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    writer.write(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    await writer.drain()
                    continue
                msg = parse_privmsg(line)
                if msg:
                    res = self.on_message(msg[0], time.time(), msg[1], msg[2])
                    if asyncio.iscoroutine(res):
                        await res
        finally:
            writer.close()


class TwitchHelix:
    """Live status + clip counts. Only used if a client id/secret is configured."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = ""
        self._token_exp = 0.0

    def _get(self, url: str) -> dict:
        if time.time() > self._token_exp - 60:
            body = urllib.parse.urlencode({
                "client_id": self.client_id, "client_secret": self.client_secret,
                "grant_type": "client_credentials"}).encode()
            with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", body, timeout=15) as r:
                tok = json.load(r)
            self._token, self._token_exp = tok["access_token"], time.time() + tok["expires_in"]
        req = urllib.request.Request(url, headers={
            "Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    async def live_logins(self, logins: list[str]) -> set[str]:
        live: set[str] = set()
        for i in range(0, len(logins), 100):  # API takes 100 logins per request
            q = "&".join(f"user_login={urllib.parse.quote(l)}" for l in logins[i:i + 100])
            data = await asyncio.to_thread(self._get, f"https://api.twitch.tv/helix/streams?{q}&first=100")
            live |= {s["user_login"].lower() for s in data.get("data", [])}
        return live
