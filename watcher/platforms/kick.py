"""Kick: live status from Kick's public channel API, chat over Kick's Pusher websocket.

Kick sits behind Cloudflare, which blocks normal Python HTTP clients. curl_cffi sends
requests that look like a real Chrome browser, which usually gets through. Datacenter
IPs (like Oracle) can still be blocked on some days; see README → Troubleshooting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from watcher.platforms.base import Platform, streamlink

log = logging.getLogger("kick")

_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def parse_channel(data: dict) -> tuple[int | None, dict | None]:
    """Kick channel API JSON -> (chatroom_id, live_info or None)."""
    chatroom = (data.get("chatroom") or {}).get("id")
    ls = data.get("livestream")
    live = None
    if ls and ls.get("is_live", True):
        live = {"title": ls.get("session_title", ""), "viewers": ls.get("viewer_count", 0),
                "started_at": ls.get("created_at")}
    return chatroom, live


def parse_pusher(frame: str) -> tuple[int, str, str] | None:
    """Pusher frame -> (chatroom_id, username, text) for chat messages, else None."""
    try:
        msg = json.loads(frame)
    except ValueError:
        return None
    if msg.get("event") != "App\\Events\\ChatMessageEvent":
        return None
    data = msg.get("data")
    if isinstance(data, str):
        data = json.loads(data)
    sender = (data.get("sender") or {}).get("username", "?")
    text = _EMOTE.sub(lambda m: m.group(1), data.get("content", ""))   # [emote:123:KEKW] -> KEKW
    return int(data.get("chatroom_id", 0)), sender, text


class Kick(Platform):
    name = "kick"

    def __init__(self, logins, on_message, pusher_key: str, pusher_cluster: str):
        super().__init__(logins, on_message)
        self.ws_url = (f"wss://ws-{pusher_cluster}.pusher.com/app/{pusher_key}"
                       "?protocol=7&client=js&version=8.4.0&flash=false")
        self.chatrooms: dict[str, int] = {}     # login -> chatroom id
        self._last_info: dict[str, dict] = {}

    def _fetch(self, login: str) -> dict:
        from curl_cffi import requests   # imported lazily: only needed if Kick channels are used
        r = requests.get(f"https://kick.com/api/v2/channels/{login}", impersonate="chrome", timeout=15)
        r.raise_for_status()
        return r.json()

    async def _channel(self, login: str) -> dict | None:
        try:
            data = await asyncio.to_thread(self._fetch, login)
        except Exception as e:
            log.warning("kick API failed for %s: %s", login, e)
            return None
        chatroom, live = parse_channel(data)
        if chatroom:
            self.chatrooms[login] = chatroom
        return {"live": live}

    async def live_status(self) -> dict[str, dict] | None:
        live = {}
        for login in self.logins:
            res = await self._channel(login)
            if res is None:   # API blocked: keep last known state instead of flapping offline
                if login in self._last_info:
                    live[login] = self._last_info[login]
                continue
            if res["live"]:
                live[login] = res["live"]
        self._last_info = live
        return live

    async def run_chat(self) -> None:
        import websockets
        backoff = 1
        while True:
            missing = [l for l in self.logins if l not in self.chatrooms]
            for login in missing:
                await self._channel(login)
            if not self.chatrooms:
                log.warning("no Kick chatroom ids yet (API blocked?); retrying in 60s")
                await asyncio.sleep(60)
                continue
            by_room = {v: k for k, v in self.chatrooms.items()}
            try:
                async with websockets.connect(self.ws_url, ping_interval=60) as ws:
                    for room in by_room:
                        await ws.send(json.dumps({"event": "pusher:subscribe",
                                                  "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
                    self.chat_connected = set(by_room.values())
                    log.info("joined %d kick chats", len(by_room))
                    backoff = 1
                    resolve_at = time.time() + 300
                    async for frame in ws:
                        if '"pusher:ping"' in frame:
                            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            continue
                        msg = parse_pusher(frame)
                        if msg and msg[0] in by_room:
                            self.on_message("kick", by_room[msg[0]], time.time(), msg[1], msg[2])
                        if time.time() > resolve_at and len(self.chatrooms) < len(self.logins):
                            break   # reconnect to pick up channels whose ids we just learned
            except Exception as e:
                log.warning("kick chat disconnected (%s); retrying in %ss", e, backoff)
            self.chat_connected = set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def record_cmd(self, login: str, info: dict | None, quality: str) -> list[str]:
        return streamlink(f"https://kick.com/{login}", quality)
