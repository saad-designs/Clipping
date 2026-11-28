"""Clip from finished streams: no 24/7 computer needed.

Each sweep looks at every streamer in the active campaigns, finds streams that ended since
last time (Twitch VODs, YouTube past streams, Kick VODs), picks the best moments, and
downloads only those moments. They then go through the normal director + render pipeline.

How moments are picked, per platform:
  Twitch   chat replay (what chat spammed, how hard) + clips viewers made (exact offsets)
  YouTube  chat replay of past live streams (yt-dlp live_chat); audio if there's none
  Kick     no public chat replay: audio loudness of a low-quality copy
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from shared.hype_tokens import is_noise
from studio import vod
from watcher import audio
from watcher.scorer import phrase

log = logging.getLogger("sweep")

GQL = "https://gql.twitch.tv/gql"
PRE, POST = 35, 25          # seconds kept around each moment (the director trims later)
VIEWER_CLIP_MIN_VIEWS = 20  # a viewer clip this popular gets judged even if the scout passed over it
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}


# ----------------------------------------------------------------------------- helpers
def _gql(query: str) -> dict:
    req = urllib.request.Request(GQL, data=json.dumps({"query": query}).encode(),
                                 headers={"Client-Id": vod.TWITCH_GQL_CLIENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def hls_section(m3u8_url: str, start: float, end: float, out: Path) -> float:
    """Download only the HLS segments covering [start, end] and join them into `out` (mp4).

    Returns the clip's real start time in the stream (the first segment's start), so callers
    can place the moment inside the clip. Works through proxies (plain HTTP), no ffmpeg networking.
    """
    def get(url: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return r.read()

    text = get(m3u8_url).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:          # master playlist: take the first (best-listed) variant
        variant = next(l for l in text.splitlines() if l and not l.startswith("#"))
        m3u8_url = urllib.parse.urljoin(m3u8_url, variant)
        text = get(m3u8_url).decode("utf-8", "replace")
    t, dur, picked = 0.0, None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            if t + dur > start and t < end:
                picked.append((t, urllib.parse.urljoin(m3u8_url, line)))
            t += dur
            dur = None
            if t >= end:
                break
    if not picked:
        raise ValueError(f"no segments between {start:.0f}s and {end:.0f}s")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, (_, url) in enumerate(picked):
            f = Path(tmp) / f"{i:05d}.ts"
            f.write_bytes(get(url))
            files.append(f)
        lst = Path(tmp) / "list.txt"
        lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files))
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"joining segments failed: {r.stderr[-300:]}")
    return picked[0][0]


def media_url(page_url: str, fmt: str = "best[height<=1080]/best") -> str:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": fmt}) as y:
        info = y.extract_info(page_url, download=False)
    return info["url"]


def top_chat(chat: list, at: float) -> list:
    c = Counter()
    for t, user, text in chat:
        if at - 2 <= t <= at + 15 and not is_noise(user, text):
            p = phrase(text)
            if p:
                c[p] += 1
    return [[p, n] for p, n in c.most_common(5) if n >= 2]


# ----------------------------------------------------------------------------- finding streams
def _chapters(node: dict) -> list[tuple[float, float, str]]:
    """[(start, end, game)] for a Twitch VOD. Streamers switch games mid-stream, and the VOD's own
    category is only the one it started with, so the chapter markers are the truth."""
    duration = float(node.get("lengthSeconds") or 0)
    marks = []
    for e in ((node.get("moments") or {}).get("edges") or []):
        m = e.get("node") or {}
        game = (((m.get("details") or {}).get("game")) or {}).get("name") or ""
        marks.append((float(m.get("positionMilliseconds") or 0) / 1000, game))
    if not marks:
        return [(0.0, duration, ((node.get("game") or {}).get("name")) or "")]
    marks.sort()
    return [(start, marks[i + 1][0] if i + 1 < len(marks) else duration, game) for i, (start, game) in enumerate(marks)]


def allowed_ranges(v: dict, games: list[str]) -> list[tuple[float, float]] | None:
    """Parts of a VOD that show one of the campaign's games. None = no filter (clip anything).

    A part counts when its game name contains one of `games` (case-insensitive). When Twitch has
    no game for a part (or the platform gives none), the stream title decides instead.
    """
    terms = [g.strip().lower() for g in games or [] if g and g.strip()]
    if not terms:
        return None
    hit = lambda s: any(t in (s or "").lower() for t in terms)
    parts = v.get("chapters") or [(0.0, v.get("duration") or 0.0, "")]
    return [(s, e) for s, e, game in parts if hit(game) or (not game and hit(v.get("title")))]


def twitch_vods(login: str, since: float) -> tuple[list[dict], list[dict]]:
    """Finished VODs newer than `since`, and the channel's popular clips this week (with offsets)."""
    q = ('query { user(login:"%s") { videos(first: 10, type: ARCHIVE) { edges { node { id title lengthSeconds createdAt status '
         'game { name } moments(momentRequestType: VIDEO_CHAPTER_MARKERS) { edges { node { positionMilliseconds '
         'details { ... on GameChangeMomentDetails { game { name } } } } } } } } } '
         'clips(first: 50, criteria:{period: LAST_WEEK, sort: VIEWS_DESC}) { edges { node { viewCount durationSeconds '
         'videoOffsetSeconds video { id } } } } } }' % login)
    user = (_gql(q).get("data") or {}).get("user") or {}
    vods = []
    for e in ((user.get("videos") or {}).get("edges") or []):
        n = e["node"]
        if n.get("status") == "RECORDED" and _ts(n["createdAt"]) > since:
            vods.append({"id": n["id"], "title": n.get("title", ""), "duration": float(n.get("lengthSeconds") or 0),
                         "created": _ts(n["createdAt"]), "url": f"https://www.twitch.tv/videos/{n['id']}",
                         "chapters": _chapters(n)})
    clips = [e["node"] for e in ((user.get("clips") or {}).get("edges") or []) if e["node"].get("video")]
    return vods, clips


def youtube_vods(handle: str, since: float) -> list[dict]:
    import yt_dlp
    base = f"https://www.youtube.com/channel/{handle}" if handle.startswith("UC") else f"https://www.youtube.com/@{handle}"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 8}) as y:
        info = y.extract_info(base + "/streams", download=False)
    out = []
    for e in info.get("entries") or []:
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        out.append({"id": e["id"], "title": e.get("title", ""), "duration": float(e.get("duration") or 0),
                    "created": float(e.get("timestamp") or 0), "url": f"https://www.youtube.com/watch?v={e['id']}"})
    # flat listings often lack dates: keep the newest few and let the "seen" list skip old ones
    return [v for v in out if not v["created"] or v["created"] > since][:4]


def kick_vods(slug: str, since: float) -> list[dict]:
    from curl_cffi import requests
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}/videos", impersonate="chrome", timeout=20)
    r.raise_for_status()
    out = []
    for v in r.json() or []:
        video = v.get("video") or {}
        created = _ts((v.get("created_at") or "").replace(" ", "T")) if v.get("created_at") else 0
        if v.get("is_live") or not video.get("uuid") or created <= since:
            continue
        out.append({"id": video["uuid"], "title": v.get("session_title", ""), "duration": float(v.get("duration") or 0) / 1000,
                    "created": created, "url": f"https://kick.com/video/{video['uuid']}", "m3u8": v.get("source")})
    return out


# ----------------------------------------------------------------------------- picking moments
def pick_moments(platform: str, v: dict, clips: list[dict], top: int, workdir: Path,
                 allowed: list[tuple[float, float]] | None = None) -> tuple[list[dict], list]:
    """[{"at", "score", "viewer_clips"}], plus the chat replay (for top_chat).

    `allowed`: only pick moments inside these (start, end) ranges, e.g. the campaign's game.
    """
    chat: list = []
    if platform == "twitch":
        chat = vod.twitch_chat(v["id"])
    elif platform == "youtube":
        chat = _youtube_chat(v["url"], workdir)
    duration = v["duration"] or (max((t for t, _, _ in chat), default=0) + 30)

    scored: list[tuple[float, float]] = []
    if len(chat) > 200:
        scored = vod.score_with_chat(chat, [], duration)
    # clips viewers made of this VOD: strong evidence; boost around their offsets
    vc = [c for c in clips if (c.get("video") or {}).get("id") == v["id"] and c.get("videoOffsetSeconds") is not None]
    for c in vc:
        at = float(c["videoOffsetSeconds"]) + float(c.get("durationSeconds") or 30) * 0.6
        scored.append((at, 8.0 + min(c.get("viewCount", 0), 5000) / 500))
    if len(scored) < top and platform != "twitch":       # no chat replay: fall back to the audio
        scored += _audio_scores(platform, v, workdir)
    if allowed is not None:
        scored = [(at, s) for at, s in scored if any(a <= at <= b for a, b in allowed)]
        vc = [c for c in vc if any(a <= float(c["videoOffsetSeconds"]) <= b for a, b in allowed)]
    picks = vod.pick(scored, top, 90, duration)
    moments = []
    for at, score in picks:
        n = sum(1 for c in vc if abs(float(c["videoOffsetSeconds"]) - at) < 60)
        moments.append({"at": at, "score": round(score, 2), "viewer_clips": n})
    return moments, chat


def _youtube_chat(url: str, workdir: Path) -> list:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "writesubtitles": True,
            "subtitleslangs": ["live_chat"], "outtmpl": str(workdir / "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    f = ((info.get("requested_subtitles") or {}).get("live_chat") or {}).get("filepath")
    return vod.parse_youtube_replay(Path(f)) if f and Path(f).exists() else []


def _audio_scores(platform: str, v: dict, workdir: Path) -> list:
    """Audio loudness of a small copy (audio-only or the lowest quality)."""
    src = workdir / f"audio_{v['id']}.m4a"
    fmt = "bestaudio[abr<=160]/worstaudio/worst"
    subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", fmt, "-x", "--audio-format", "m4a", "-o", str(src), v["url"]],
                   check=True, capture_output=True, timeout=3600)
    loud = audio.analyze(src, 0, timeout=1800)
    src.unlink(missing_ok=True)
    return vod.score_without_chat(loud, [], v["duration"] or (loud[-1][0] if loud else 0))


def scout_moments(store, key: str, ch: dict, camp: dict, v: dict, clips: list, top: int,
                  allowed, on_status) -> tuple[list[dict], list]:
    """The director watches the (game) parts of the stream and picks the moments itself."""
    from studio import scan
    chat = vod.twitch_chat(v["id"]) if ch["platform"] == "twitch" else []
    ranges = allowed if allowed is not None else [(0.0, v["duration"])]
    found = scan.scan_vod(store, key, v, ranges, camp, ch["login"], chat, clips, on_status)
    good = sorted((m for m in found if m["score"] >= scan.MIN_SCORE), key=lambda m: -m["score"])[:top]
    log.info("scout found %d moments in %s (%d good enough)", len(found), v["id"], len(good))
    # Safety net: the clips viewers made most are real evidence. If the scout passed over one,
    # still hand it to the editing director, which watches it properly and can turn it down.
    vc = sorted((c for c in clips if (c.get("video") or {}).get("id") == v["id"] and c.get("videoOffsetSeconds") is not None
                 and int(c.get("viewCount") or 0) >= VIEWER_CLIP_MIN_VIEWS), key=lambda c: -int(c.get("viewCount") or 0))
    for c in vc[:2]:
        at = float(c["videoOffsetSeconds"]) + float(c.get("durationSeconds") or 30) * 0.6
        inside = ranges is None or any(a <= at <= b for a, b in ranges)
        if inside and all(abs(at - m["at"]) > 45 for m in good):
            good.append({"start": float(c["videoOffsetSeconds"]), "at": at,
                         "end": float(c["videoOffsetSeconds"]) + float(c.get("durationSeconds") or 30),
                         "score": scan.MIN_SCORE, "what": f"viewers clipped this ({c.get('viewCount')} views); the scout didn't pick it",
                         "why": "viewer clip"})
    out = []
    for m in sorted(good, key=lambda m: m["at"]):
        n = sum(1 for c in clips if (c.get("video") or {}).get("id") == v["id"]
                and abs(float(c.get("videoOffsetSeconds") or -999) - m["at"]) < 60)
        out.append({**m, "viewer_clips": n, "scout": {"what": m["what"], "why": m["why"], "score": m["score"]}})
    return out, chat


def window(m: dict) -> tuple[float, float]:
    """Which part of the stream to download for a moment: the scout's setup..reaction with room
    for the director to trim (30-100 s), or the fixed window around a chat spike."""
    at = m["at"]
    if "start" not in m:
        return max(0.0, at - PRE), at + POST
    start, stop = min(m["start"], at - 5) - 6, max(m["end"], at + 5) + 6
    if stop - start > 100:
        start, stop = max(start, at - 60), min(stop, max(start, at - 60) + 100)
    if stop - start < 30:
        pad = (30 - (stop - start)) / 2
        start, stop = start - pad, stop + pad
    return max(0.0, start), stop


# ----------------------------------------------------------------------------- main
def sweep(store, max_age_days: float = 3.0, on_status=lambda s: None) -> dict:
    """Check every streamer in active campaigns once. Returns {"new_clips", "vods", "errors"}."""
    result = {"new_clips": 0, "vods": 0, "errors": []}
    seen = set(store.seen_vods())
    clips_dir = store.root / "clips"
    for camp in store.campaigns():
        if not camp["active"]:
            continue
        top = int(camp.get("clips_per_stream") or 5)
        for ch in camp["channels"]:
            key = f"{ch['platform']}:{ch['login']}"
            since = max(store.last_sweep(key), time.time() - max_age_days * 86400)
            try:
                on_status(f"checking {key} for finished streams")
                clips: list = []
                if ch["platform"] == "twitch":
                    vods, clips = twitch_vods(ch["login"], since)
                elif ch["platform"] == "youtube":
                    vods = youtube_vods(ch["login"], since)
                elif ch["platform"] == "kick":
                    vods = kick_vods(ch["login"], since)
                else:
                    continue
                for v in vods:
                    vkey = f"{ch['platform']}:{v['id']}:{camp['name']}"
                    if vkey in seen:
                        continue
                    allowed = allowed_ranges(v, camp.get("games") or [])
                    if allowed is not None and not allowed:
                        log.info("skipping %s %s: not one of %s", key, v["title"][:50], camp["games"])
                        continue
                    on_status(f"finding the best moments in {key}: {v['title'][:50]}")
                    s = store.settings()
                    if s.get("director") == "gemini" and s.get("gemini_api_key") and ch["platform"] in ("twitch", "kick"):
                        moments, chat = scout_moments(store, s["gemini_api_key"], ch, camp, v, clips, top, allowed, on_status)
                    else:
                        with tempfile.TemporaryDirectory() as tmp:
                            moments, chat = pick_moments(ch["platform"], v, clips, top, Path(tmp), allowed)
                    m3u8 = (v.get("m3u8") or media_url(v["url"])) if moments else None
                    for i, m in enumerate(moments, 1):
                        on_status(f"downloading moment {i}/{len(moments)} from {key}")
                        out = clips_dir / f"{ch['platform']}_{v['id']}_{int(m['at'])}.mp4"
                        start, stop = window(m)
                        if ch["platform"] == "youtube":   # DASH, not HLS: let yt-dlp cut it
                            real_start = _ytdlp_section(v["url"], start, stop, out)
                        else:
                            real_start = hls_section(m3u8, start, stop, out)
                        sig = {"z_hype": min(m["score"], 30), "top_chat": top_chat(chat, m["at"]) if chat else []}
                        meta = {"moment_offset": round(m["at"] - real_start, 2), "signals": sig, "score": m["score"],
                                "viewer_clips": m["viewer_clips"], "vod": v["url"], "vod_time": round(m["at"], 1),
                                "vod_title": v["title"]}
                        if m.get("scout"):     # what the scout saw, in this clip's own time
                            meta["scout"] = {**m["scout"], "start": round(m["start"] - real_start, 1),
                                             "peak": round(m["at"] - real_start, 1), "end": round(m["end"] - real_start, 1)}
                        cid = store.add_clip(camp["name"], str(out), key, "vod", None, meta)
                        result["new_clips"] += 1 if cid else 0
                    store.mark_vod(vkey)
                    result["vods"] += 1
                store.set_last_sweep(key, time.time())
            except Exception as e:
                log.exception("sweep failed for %s", key)
                result["errors"].append(f"{key}: {type(e).__name__}: {str(e)[:200]}")
    return result


def _ytdlp_section(url: str, start: float, end: float, out: Path) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpl = str(Path(tmp) / "sec.%(ext)s")
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "best[height<=1080]/best", "--download-sections",
                        f"*{start:.0f}-{end:.0f}", "--merge-output-format", "mp4", "-o", tmpl, url],
                       check=True, capture_output=True, timeout=900)
        f = next(Path(tmp).glob("sec.*"))
        shutil.move(str(f), out)
    return start
