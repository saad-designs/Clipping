"""YouTube Live: live detection from the channel's /live page, chat via YouTube's own
web-client API (the same calls the live-chat popout makes), recording via yt-dlp.

This uses no API key and no quota, but it is unofficial: if YouTube changes its page,
`parse_*` here is what needs updating. Datacenter IPs often get "confirm you're not a
bot"; set youtube.cookies_file in settings.yaml (see README).
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import re
import time
import urllib.request

from watcher.platforms.base import Platform

log = logging.getLogger("youtube")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def channel_url(login: str) -> str:
    return f"https://www.youtube.com/channel/{login}" if login.startswith("UC") else f"https://www.youtube.com/@{login}"


def parse_live_page(html: str) -> dict | None:
    """The /live page of a channel. Returns {"video_id", "title"} if it's live right now."""
    if '"isLiveNow":true' not in html and '"isLive":true' not in html:
        return None
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html) \
        or re.search(r'"videoDetails":\{"videoId":"([\w-]{11})"', html)
    if not m:
        return None
    title = re.search(r'<meta name="title" content="([^"]*)"', html)
    return {"video_id": m.group(1), "title": title.group(1) if title else ""}


def parse_chat_page(html: str) -> tuple[str, str, str] | None:
    """live_chat popout page -> (api_key, client_version, first continuation)."""
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html) or \
        re.search(r'"clientVersion":"([^"]+)"', html)
    # The block holds nested objects (invalidationId:{...}) before the token, so search forward from the key.
    start = re.search(r'"(?:invalidation|timed|reload)ContinuationData":\{', html)
    cont = re.search(r'"continuation":"([^"]+)"', html[start.end():start.end() + 1500]) if start else None
    if not (key and ver and cont):
        return None
    return key.group(1), ver.group(1), cont.group(1)


def _runs_text(runs: list[dict]) -> str:
    out = []
    for r in runs:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            out.append((e.get("shortcuts") or [e.get("emojiId", "")])[0] if e.get("isCustomEmoji") else e.get("emojiId", ""))
    return "".join(out)


def parse_chat_response(data: dict) -> tuple[list[tuple[float, str, str]], str | None, int]:
    """get_live_chat JSON -> ([(ts, user, text)], next_continuation, wait_ms)."""
    lcc = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
    msgs = []
    for action in lcc.get("actions", []):
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer")
        if not r:
            continue
        ts = int(r.get("timestampUsec", time.time() * 1e6)) / 1e6
        user = (r.get("authorName") or {}).get("simpleText", "?")
        msgs.append((ts, user, _runs_text((r.get("message") or {}).get("runs", []))))
    nxt, wait = None, 5000
    for c in lcc.get("continuations", []):
        for v in c.values():
            if isinstance(v, dict) and "continuation" in v:
                nxt, wait = v["continuation"], int(v.get("timeoutMs", 5000))
                break
        if nxt:
            break
    return msgs, nxt, wait


class YouTube(Platform):
    name = "youtube"

    def __init__(self, logins, on_message, cookies_file: str = ""):
        super().__init__(logins, on_message)
        self.cookies_file = cookies_file
        jar = http.cookiejar.MozillaCookieJar()
        if cookies_file:
            try:
                jar.load(cookies_file, ignore_discard=True, ignore_expires=True)
            except OSError as e:
                log.warning("couldn't load youtube cookies %s: %s", cookies_file, e)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.chat_tasks: dict[str, asyncio.Task] = {}

    def _get(self, url: str, body: dict | None = None) -> str:
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                   "Cookie": "SOCS=CAI"}   # skip the EU cookie-consent interstitial
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with self.opener.open(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")

    async def live_status(self) -> dict[str, dict] | None:
        live = {}
        for login in self.logins:
            try:
                html = await asyncio.to_thread(self._get, channel_url(login) + "/live")
            except Exception as e:
                log.warning("youtube live check failed for %s: %s", login, e)
                continue
            info = parse_live_page(html)
            if info:
                live[login] = info
        return live

    async def run_chat(self) -> None:
        # Chat only exists while a channel is live; on_live_change starts/stops pollers.
        while True:
            await asyncio.sleep(3600)

    def on_live_change(self, login: str, info: dict | None) -> None:
        task = self.chat_tasks.pop(login, None)
        if task:
            task.cancel()
        self.chat_connected.discard(login)
        if info:
            self.chat_tasks[login] = asyncio.get_running_loop().create_task(self._poll(login, info["video_id"]))

    async def _poll(self, login: str, video_id: str) -> None:
        while True:
            try:
                page = await asyncio.to_thread(
                    self._get, f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}")
                parsed = parse_chat_page(page)
                if not parsed:
                    raise RuntimeError("live chat page format not recognised (chat disabled?)")
                key, ver, cont = parsed
                self.chat_connected.add(login)
                log.info("youtube chat connected: %s (%s)", login, video_id)
                while cont:
                    body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver}}, "continuation": cont}
                    raw = await asyncio.to_thread(
                        self._get, f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}", body)
                    msgs, cont, wait = parse_chat_response(json.loads(raw))
                    for ts, user, text in msgs:
                        self.on_message("youtube", login, ts, user, text)
                    await asyncio.sleep(min(max(wait, 1000), 10000) / 1000)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("youtube chat for %s failed (%s); retrying in 30s", login, e)
            self.chat_connected.discard(login)
            await asyncio.sleep(30)

    def record_cmd(self, login: str, info: dict | None, quality: str) -> list[str]:
        url = f"https://www.youtube.com/watch?v={info['video_id']}" if info else channel_url(login) + "/live"
        # quality uses streamlink syntax ("720p60,best"); take the first height for yt-dlp
        h = re.match(r"(\d+)p", quality)
        fmt = f"best[height<={h.group(1)}]/best" if h else "best"
        cmd = ["yt-dlp", "--quiet", "--no-warnings", "--no-live-from-start",
               "--hls-use-mpegts", "-f", fmt, "-o", "-"]
        if self.cookies_file:
            cmd += ["--cookies", self.cookies_file]
        return cmd + [url]
