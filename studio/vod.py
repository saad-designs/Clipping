"""VOD mode: find the best moments in a finished stream / podcast / long video.

    python -m studio vod <url or file> --campaign NAME [--top 8]

1. Download with yt-dlp (or use a local file).
2. Signals: chat replay where it exists (YouTube past streams, Twitch VODs), audio loudness
   for the whole file, and, when there's no chat, hype keywords from a quick transcript.
3. Rank every 5-second bucket, keep the best N at least 90s apart.
4. Cut each into Campaigns/<campaign>/inbox/ with a sidecar, so the normal editor finishes them.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.request
from pathlib import Path

from shared.config import ScorerConfig
from studio.editor import transcribe
from studio.sync import campaign_dirs, safe, write_sidecar
from watcher import audio
from watcher.scorer import ChannelScorer, _z

log = logging.getLogger("vod")

BUCKET = 5
PRE, POST = 35, 25
KEYWORDS = re.compile(r"\b(no way|oh my god|oh my gosh|what the|holy|let'?s go|insane|crazy|bro|dude|"
                      r"ha(ha)+|lmao|laugh(s|ing|ter)?|scream(s|ing)?|wow|wtf|shut up|are you serious)\b", re.I)


# ---------- download ---------------------------------------------------------------
def download(url: str, dest_dir: Path, cookies: str | None = None) -> tuple[Path, Path | None, dict]:
    """Returns (video file, chat replay file or None, info)."""
    import yt_dlp
    opts = {"format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4",
            "outtmpl": str(dest_dir / "%(extractor_key)s_%(id)s.%(ext)s"),
            "writesubtitles": True, "subtitleslangs": ["live_chat"], "quiet": True, "no_warnings": True}
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    video = Path(info["requested_downloads"][0]["filepath"])
    chat = (info.get("requested_subtitles") or {}).get("live_chat", {}).get("filepath")
    return video, Path(chat) if chat and Path(chat).exists() else None, info


# ---------- chat replay ------------------------------------------------------------
def parse_youtube_replay(path: Path) -> list[tuple[float, str, str]]:
    """yt-dlp's live_chat.json (one JSON object per line) -> [(offset_s, user, text)]."""
    from watcher.platforms.youtube import _runs_text
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        replay = obj.get("replayChatItemAction") or {}
        offset = int(replay.get("videoOffsetTimeMsec", 0)) / 1000
        for action in replay.get("actions", []):
            r = ((action.get("addChatItemAction") or {}).get("item") or {}).get("liveChatTextMessageRenderer")
            if r:
                out.append((offset, (r.get("authorName") or {}).get("simpleText", "?"),
                            _runs_text((r.get("message") or {}).get("runs", []))))
    return out


TWITCH_GQL_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # Twitch's public web-client id
TWITCH_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def parse_twitch_comments(page: dict) -> tuple[list[tuple[float, str, str]], str | None]:
    comments = ((page.get("data") or {}).get("video") or {}).get("comments") or {}
    out, cursor = [], None
    for edge in comments.get("edges", []):
        n = edge.get("node") or {}
        text = "".join(f.get("text", "") for f in (n.get("message") or {}).get("fragments", []))
        out.append((float(n.get("contentOffsetSeconds", 0)), ((n.get("commenter") or {}).get("displayName") or "?"), text))
        cursor = edge.get("cursor")
    if not (comments.get("pageInfo") or {}).get("hasNextPage"):
        cursor = None
    return out, cursor


def twitch_chat(video_id: str, max_pages: int = 5000) -> list[tuple[float, str, str]]:
    """Twitch VOD chat via Twitch's web GQL. Unofficial: returns [] if Twitch blocks it."""
    msgs, cursor, first = [], None, True
    for _ in range(max_pages):
        variables = {"videoID": video_id, **({"cursor": cursor} if cursor else {"contentOffsetSeconds": 0})}
        body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_COMMENTS_HASH}}}]
        req = urllib.request.Request("https://gql.twitch.tv/gql", data=json.dumps(body).encode(),
                                     headers={"Client-Id": TWITCH_GQL_CLIENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                page = json.load(r)[0]
        except Exception as e:
            if first:
                log.warning("twitch chat replay unavailable (%s); using audio only", e)
            break
        first = False
        batch, cursor = parse_twitch_comments(page)
        msgs += batch
        if not cursor:
            break
    return msgs


# ---------- scoring ----------------------------------------------------------------
def score_with_chat(chat, loudness, duration: float, extra_tokens=()) -> list[tuple[float, float]]:
    cfg = ScorerConfig(min_messages=3, warmup_buckets=12, score_delay_seconds=0)
    sc = ChannelScorer("vod", cfg, set(extra_tokens), collect=True)
    for ts, lufs in loudness:
        sc.add_audio(ts, lufs)
    for ts, user, text in sorted(chat):
        sc.add_message(ts, user, text)
    sc.tick(duration + 60)
    # chat has to react at least a little; volume alone isn't a moment (same rule as live)
    return [(at, score) for at, score, zs in sc.collected if zs.get("hype", 0) >= 1.0]


def score_without_chat(loudness, words, duration: float) -> list[tuple[float, float]]:
    peaks: dict[int, float] = {}
    for ts, lufs in loudness:
        b = int(ts // BUCKET)
        peaks[b] = max(peaks.get(b, -70.0), lufs)
    kw: dict[int, int] = {}
    for w in words:
        if KEYWORDS.search(w.text):
            kw[int(w.start // BUCKET)] = kw.get(int(w.start // BUCKET), 0) + 1
    out, history = [], []
    for b in range(int(duration // BUCKET) + 1):
        p = peaks.get(b, -70.0)
        z = _z(p, history[-120:], 3.0) if len(history) >= 12 else 0.0
        out.append((b * BUCKET + BUCKET / 2, max(z, 0) + 1.5 * min(kw.get(b, 0), 4)))
        history.append(p)
    return out


def pick(scored: list[tuple[float, float]], top: int, gap: float, duration: float) -> list[tuple[float, float]]:
    chosen: list[tuple[float, float]] = []
    for t, s in sorted(scored, key=lambda x: -x[1]):
        if s <= 0 or t < 5 or t > duration - 3:
            continue
        if all(abs(t - c) >= gap for c, _ in chosen):
            chosen.append((t, s))
        if len(chosen) >= top:
            break
    return sorted(chosen)


# ---------- main -------------------------------------------------------------------
def run(source: str, campaign: str, cfg: dict, top: int | None = None, rules: dict | None = None,
        cookies: str | None = None, transcribe_no_chat: bool = True) -> list[Path]:
    dirs = campaign_dirs(cfg["output_dir"], campaign)
    vcfg = cfg["vod"]
    top = top or int(vcfg["top"])
    local = Path(source)
    if local.exists():
        video, chat_file, info = local, None, {"id": local.stem, "extractor_key": "file"}
    else:
        log.info("downloading %s …", source)
        video, chat_file, info = download(source, dirs["vods"], cookies)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(video)], capture_output=True, text=True, check=True).stdout.strip())

    chat = []
    if chat_file:
        chat = parse_youtube_replay(chat_file)
    elif info.get("extractor_key", "").lower().startswith("twitch"):
        chat = twitch_chat(str(info["id"]).lstrip("v"))
    log.info("analysing %.0f min of audio%s …", duration / 60, f" + {len(chat)} chat messages" if chat else "")
    loudness = audio.analyze(video, 0, timeout=None)

    scored = score_with_chat(chat, loudness, duration) if len(chat) > 200 else []
    if len(scored) < top:   # little or no chat: fall back to audio + keywords
        words = []
        if transcribe_no_chat and not chat:
            log.info("no chat replay: transcribing with whisper '%s' to find hype words (can take a while)…",
                     vcfg["whisper_model"])
            words = transcribe.transcribe(video, vcfg["whisper_model"])
        scored = score_without_chat(loudness, words, duration)
    picks = pick(scored, top, float(vcfg["min_gap_seconds"]), duration)

    out = []
    base = safe(f"{info.get('extractor_key', 'vod')}_{info['id']}")
    for n, (t, score) in enumerate(picks, 1):
        start, end = max(0.0, t - PRE), min(duration, t + POST)
        dest = dirs["inbox"] / f"{base}_{int(t // 3600):02d}h{int(t % 3600 // 60):02d}m{int(t % 60):02d}s.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-i", str(video),
                        "-t", f"{end - start:.2f}", "-c", "copy", "-avoid_negative_ts", "make_zero", str(dest)], check=True)
        write_sidecar(dest, {"id": f"{base}_{n}", "source": "vod", "vod": source, "vod_time": round(t, 1),
                             "channel": info.get("uploader_id") or info.get("channel") or "", "score": round(score, 2),
                             "moment_offset": round(t - start, 2), "campaign": campaign, "rules": rules or {}})
        log.info("picked %d/%d at %s (score %.1f) → %s", n, len(picks), f"{int(t // 60)}:{int(t % 60):02d}", score, dest.name)
        out.append(dest)
    return out
