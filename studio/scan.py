"""The director watches the whole stream to pick the moments (instead of trusting chat spikes).

Small streamers have small chats: a "spike" of 6 messages is noise, and viewer clips are rare.
So for each finished stream we download a tiny 160p copy of the parts that show the campaign's
game, cut it into ~12-minute pieces, and have Gemini watch and listen to each piece and list
every moment worth a Short, with a reason and an honest score. Chat activity and viewer clips
are passed along as hints, not as the decision.

Results are cached per stream (studio_data/scans/), piece by piece, so a dropped connection or a
closed laptop resumes where it stopped instead of starting over.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

from studio import gemini_director as gd

log = logging.getLogger("scan")

CHUNK = 1200.0       # seconds of stream per request (~8 MB at the tiny size: under the 20 MB inline limit)
MIN_SCORE = 6        # the scout's score a moment needs before we download it in full quality
OVERLAP = 45.0       # pieces overlap so a moment on the boundary isn't cut in half
LOW_FORMAT = "160p/worst[height>=140]/worst"

SCAN_SCHEMA = {
    "type": "object",
    "required": ["summary", "moments"],
    "properties": {
        "summary": {"type": "string", "description": "one or two sentences: what happens in this part of the stream"},
        "moments": {"type": "array", "description": "every moment in this video that could be a great Short; empty if none",
                    "items": {"type": "object", "required": ["start", "peak", "end", "what", "why", "score"],
                              "properties": {
                                  "start": {"type": "number", "description": "seconds from the start of THIS video where the setup begins"},
                                  "peak": {"type": "number", "description": "seconds: the payoff / biggest reaction"},
                                  "end": {"type": "number", "description": "seconds: when the reaction has landed"},
                                  "what": {"type": "string", "description": "what happens, concretely"},
                                  "why": {"type": "string", "description": "why a stranger scrolling would stop and watch to the end"},
                                  "score": {"type": "integer", "description": "1-10, strict: 5 = fine, 7 = good Short, 9+ = could go viral"},
                              }}},
    },
}

SCAN_SYSTEM = """You are the clip scout for a short-form clipping channel. You watch and listen to a piece of a finished livestream (low resolution, that's expected) and find the moments that would make great vertical Shorts/Reels for strangers who have never heard of this streamer.

What makes a moment worth clipping (from strongest):
- a genuine big reaction: screaming, crying laughing, rage, shock, panic, that the viewer can see and hear
- something funny the streamer says or does: a great line, a roast, a bit, comedic timing, irony (e.g. bragging right before failing)
- a surprising or chaotic game event WITH the streamer reacting to it
- a short self-contained story: setup -> payoff -> reaction, understandable without context
NOT worth clipping: ordinary gameplay, driving around, menus, loading, cutscenes the streamer watches quietly, mumbling, reading chat with no punchline, moments that need context to be funny.

Be strict. Most minutes of a stream have nothing. It's fine to return no moments. Only list a moment you'd stake your channel on; score honestly (5 = okay, 7 = good, 9+ = rare). Times are seconds from the start of the video you're given. A good Short moment is usually 15-60 seconds from setup to landed reaction. Follow the campaign brief (it says what content counts)."""


def low_url(page_url: str) -> str:
    from studio.sweep import media_url
    return media_url(page_url, LOW_FORMAT)


def tiny(src: Path) -> bytes:
    """Smallest copy the model can still read: 256 px, 2 fps, mono 32 kbps audio. ~1 MB/min."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "t.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vf", "scale=256:-2,fps=2",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "34", "-c:a", "aac", "-b:a", "32k", "-ac", "1",
                        "-movflags", "+faststart", str(out)], check=True, capture_output=True, timeout=600)
        return out.read_bytes()


def chunks(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for a, b in ranges:
        t = a
        while t < b - 20:
            out.append((round(t, 1), round(min(b, t + CHUNK), 1)))
            t += CHUNK - OVERLAP
    return out


def hints(a: float, b: float, chat: list, viewer_clips: list) -> str:
    """Chat activity per minute and viewer clips inside [a, b], in the piece's own time."""
    per_min = Counter(int((t - a) // 60) for t, _, _ in chat if a <= t < b)
    lines = []
    if per_min:
        avg = sum(per_min.values()) / max(1, (b - a) / 60)
        busy = [f"{m * 60:.0f}s: {n} msgs" for m, n in sorted(per_min.items()) if n >= max(3, 1.8 * avg)]
        lines.append(f"Chat: ~{avg:.0f} messages/minute on average. Busier minutes: {', '.join(busy) or 'none'}.")
    vc = [c for c in viewer_clips if a <= float(c.get("videoOffsetSeconds") or -1) < b]
    if vc:
        lines.append("Viewers made clips starting at: " + ", ".join(
            f"{float(c['videoOffsetSeconds']) - a:.0f}s ({c.get('viewCount', 0)} views)" for c in vc))
    return "\n".join(lines) or "No chat replay for this part."


def scan_piece(key: str, video: bytes, campaign: dict, streamer: str, hint: str) -> dict:
    prompt = (f"CAMPAIGN: {campaign.get('name')}\nBRIEF:\n{campaign.get('brief', '')}\n\n"
              f"STREAMER: {streamer} (pronoun: {campaign.get('pronoun') or 'they'})\n\n"
              f"HINTS (chat and viewer clips are weak signals for small streams; trust what you see and hear):\n{hint}")
    decision, model = gd.ask(key, video, prompt, schema=SCAN_SCHEMA, system=SCAN_SYSTEM)
    decision["_model"] = model
    return decision


def scan_vod(store, key: str, v: dict, ranges: list[tuple[float, float]], campaign: dict, streamer: str,
             chat: list, viewer_clips: list, on_status=lambda s: None) -> list[dict]:
    """All moments the scout found in the given parts of a VOD, in VOD time. Cached per piece."""
    cache_dir = store.root / "scans"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"{v['id']}.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    url = None
    pieces = chunks(ranges)
    found: list[dict] = []
    for i, (a, b) in enumerate(pieces, 1):
        k = f"{a:.0f}-{b:.0f}"
        if k not in cache:
            on_status(f"director is watching {streamer}'s stream: part {i}/{len(pieces)}")
            url = url or low_url(v["url"])
            with tempfile.TemporaryDirectory() as tmp:
                from studio.sweep import hls_section
                raw = Path(tmp) / "piece.mp4"
                real_a = hls_section(url, a, b, raw)
                video = tiny(raw)
            res = scan_piece(key, video, campaign, streamer, hints(real_a, b, chat, viewer_clips))
            cache[k] = {"offset": real_a, "summary": res.get("summary", ""), "moments": res.get("moments") or [],
                        "model": res.get("_model"), "at": time.time()}
            cache_file.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
            log.info("scanned %s %s: %d moments (%s)", v["id"], k, len(cache[k]["moments"]), cache[k]["model"])
        piece = cache[k]
        for m in piece["moments"]:
            try:
                off = float(piece["offset"])
                found.append({"start": off + float(m["start"]), "at": off + float(m["peak"]), "end": off + float(m["end"]),
                              "score": int(m["score"]), "what": m.get("what", ""), "why": m.get("why", "")})
            except (KeyError, TypeError, ValueError):
                continue
    return dedupe(found)


def dedupe(moments: list[dict]) -> list[dict]:
    """Pieces overlap, so the same moment can be found twice: keep the higher-scored one."""
    out: list[dict] = []
    for m in sorted(moments, key=lambda m: -m["score"]):
        if all(abs(m["at"] - o["at"]) > 20 for o in out):
            out.append(m)
    return sorted(out, key=lambda m: m["at"])
