"""The watching director: Gemini looks at the clip *with its sound* and directs the edit.

Free tier (Google AI Studio key, no card). It gets a small 480p copy of the whole source clip
plus the transcript, what chat did and the campaign brief, and returns:
  - what it saw (a timeline: what happens in the game, what the streamer does)
  - who is talking when (streamer vs game dialogue), so captions show only the streamer
  - a verdict: post it or skip it (cutscene, nothing happens, breaks the brief), and why
  - the story beats (setup, payoff, reaction) and an edit built on them: the parts to keep
    (gaps = jump cuts over dead air), punch-ins with a target (face / game) and a reason each,
    a hook that's true to the clip, captions, post text.

Models are tried in order; when one is out of free quota (429) the next takes over, and if all
fail the rule-based director does the edit so nothing stops.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from studio.editor.captions import PRESETS

log = logging.getLogger("gemini")

API = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
MODELS = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash"]
LAYOUTS = ["facecam_split", "face_crop", "blur_fit", "game_only"]
_exhausted: dict[str, float] = {}      # model -> time it ran out of free quota (skip it for a while)
_busy: dict[str, float] = {}           # model -> time it was still overloaded after retries


class QuotaError(RuntimeError):
    pass


SCHEMA = {
    "type": "object",
    "required": ["watched", "speech", "verdict", "story", "keep", "layout", "layout_why", "zooms", "hook",
                 "caption_style", "emphasis_words", "post_caption", "game_x"],
    "properties": {
        "watched": {
            "type": "object", "required": ["summary", "timeline"],
            "properties": {
                "summary": {"type": "string", "description": "2-3 sentences: what actually happens in this clip"},
                "timeline": {"type": "array", "description": "what you see and hear, roughly every 3 seconds",
                             "items": {"type": "object", "required": ["t", "game", "streamer"],
                                       "properties": {"t": {"type": "number"},
                                                      "game": {"type": "string", "description": "what happens on screen; say if it's a cutscene, menu or loading"},
                                                      "streamer": {"type": "string", "description": "the streamer's face/voice: talking, laughing, shocked, silent..."}}}},
            }},
        "speech": {"type": "array", "description": "who is talking, in time ranges",
                   "items": {"type": "object", "required": ["from", "to", "who"],
                             "properties": {"from": {"type": "number"}, "to": {"type": "number"},
                                            "who": {"type": "string", "enum": ["streamer", "game", "other"]}}}},
        "verdict": {"type": "object", "required": ["post", "score", "reason"],
                    "properties": {"post": {"type": "boolean"},
                                   "score": {"type": "integer", "description": "1-10, honest: would a stranger watch this to the end?"},
                                   "reason": {"type": "string"}}},
        "story": {"type": "object", "required": ["setup_at", "payoff_at", "reaction_at", "beats"],
                  "properties": {"setup_at": {"type": "number"}, "payoff_at": {"type": "number"},
                                 "reaction_at": {"type": "number"},
                                 "beats": {"type": "string", "description": "one line: setup -> payoff -> reaction"}}},
        "keep": {"type": "array", "description": "the parts of the source to keep, in order; gaps between them become jump cuts",
                 "items": {"type": "object", "required": ["from", "to", "why"],
                           "properties": {"from": {"type": "number"}, "to": {"type": "number"}, "why": {"type": "string"}}}},
        "layout": {"type": "string", "enum": LAYOUTS},
        "layout_why": {"type": "string"},
        "game_x": {"type": "number", "description": "0..1 horizontal focus for the gameplay crop (0.5 = centre)"},
        "zooms": {"type": "array", "description": "0-3 punch-ins, each tied to something that happens",
                  "items": {"type": "object", "required": ["at", "target", "strength", "why"],
                            "properties": {"at": {"type": "number"},
                                           "target": {"type": "string", "enum": ["face", "game"]},
                                           "strength": {"type": "number", "description": "0.08 subtle .. 0.22 strong"},
                                           "why": {"type": "string"}}}},
        "hook": {"type": "object", "required": ["text", "why"],
                 "properties": {"text": {"type": "string", "description": "2-7 words, or '' for none"}, "why": {"type": "string"}}},
        "caption_style": {"type": "string", "enum": list(PRESETS)},
        "emphasis_words": {"type": "array", "items": {"type": "string"}},
        "post_caption": {"type": "string"},
    },
}

SYSTEM = """You are the director of a short-form clipping channel. You WATCH and LISTEN to a ~1 minute piece of a livestream and decide how it becomes a vertical Reel/Short that strangers watch to the end. The campaign brief is a hard rule.

How you work:
1. Watch first. Write down what happens on screen and what the streamer does (face, voice). Notice cutscenes, menus, loading screens and game characters talking: game dialogue is NOT the streamer.
2. Judge honestly. A clip is worth posting when something happens AND the streamer reacts or says something worth hearing. A cutscene the streamer just watches silently, a menu, or nothing happening = post: false. Don't inflate scores: 5 is average, 8+ is genuinely good.
3. Find the story: setup (what makes us care), payoff (the thing that happens), reaction (the streamer's response). Most clips should end shortly after the reaction lands.
4. Direct the edit. Every move needs a reason you saw or heard:
   - keep: start right before the setup, no warm-up. Cut out dead air (nobody talking, nothing happening, more than ~1.5 s) as jump cuts, but never cut through action or a sentence. Total length must respect the campaign's min/max seconds.
   - zooms: only when they show something better. target "face" when the streamer's reaction is the point (punch in as it happens); target "game" when a small detail on screen matters. 0 zooms is fine. Never zoom just to add motion.
   - hook: a short line that makes someone stay, describing what really happens (no lies, no clickbait the clip doesn't pay off). Sound like a person, not a template. Use the streamer's pronoun given. No emoji in the hook. Must not mock or show the streamer in a negative light.
   - layout: facecam_split when there's a webcam and the reaction matters; face_crop when the person fills the frame; game_only when the webcam doesn't add anything; blur_fit when the wide frame must stay.
   - caption_style: pick one that fits the mood; avoid the recently used ones listed.
   - post_caption: one natural line in the campaign's language, may include one emoji. Required hashtags/tags are added automatically.
All times are seconds in the SOURCE clip's timeline (0 = first frame of the video you're given)."""


# ----------------------------------------------------------------------------- video
def proxy(src: Path, crf: int = 32) -> bytes:
    """A small copy for the model: 480p, 12 fps, mono audio. ~3-6 MB for a minute."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "proxy.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vf", "scale=-2:480,fps=12",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                        "-movflags", "+faststart", str(out)], check=True, capture_output=True, timeout=300)
        data = out.read_bytes()
    if len(data) > 14_000_000 and crf < 40:
        return proxy(src, crf + 4)
    return data


# ----------------------------------------------------------------------------- API
def _post(body: dict, key: str, timeout: int = 300) -> dict:
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-goog-api-key": key, "Content-Type": "application/json", "Api-Revision": API_REVISION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code == 429:
            raise QuotaError(detail) from None
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


BUSY_WAITS = (10, 30)      # seconds to wait when a model is overloaded (HTTP 503) before trying again
BUSY_COOLDOWN = 600        # still busy after that: skip it for 10 minutes so the next clips don't wait too


def _post_patiently(body: dict, key: str) -> dict:
    """Free-tier requests get turned away when a model is busy ("high demand"); that passes in
    seconds to minutes, so wait and retry the better model before falling back to a weaker one."""
    for wait in (*BUSY_WAITS, None):
        try:
            return _post(body, key)
        except RuntimeError as e:
            if wait is None or "HTTP 503" not in str(e):
                raise
            log.info("%s is busy; trying again in %ss", body["model"], wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def response_text(resp: dict) -> str:
    """The model's final text from an Interactions response (skips thought steps)."""
    if isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"]
    texts = []
    for step in resp.get("steps") or []:
        if "thought" in str(step.get("type", "")).lower():
            continue
        content = step.get("content")
        for c in content if isinstance(content, list) else [content]:
            if isinstance(c, dict) and c.get("text") and "thought" not in str(c.get("type", "")).lower():
                texts.append(c["text"])
    for c in resp.get("outputs") or []:
        if isinstance(c, dict) and c.get("text"):
            texts.append(c["text"])
    return texts[-1] if texts else ""


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def ask(key: str, video: bytes, prompt: str, models: list[str] | None = None) -> tuple[dict, str]:
    """Run the director on the first model with quota left. Returns (decision, model)."""
    errors = []
    for model in models or MODELS:
        if time.time() - _exhausted.get(model, 0) < 3600 or time.time() - _busy.get(model, 0) < BUSY_COOLDOWN:
            continue
        body = {"model": model, "store": False, "system_instruction": SYSTEM,
                "input": [{"type": "video", "data": base64.b64encode(video).decode(), "mime_type": "video/mp4"},
                          {"type": "text", "text": prompt}],
                "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
                "generation_config": {"thinking_level": "high"}}
        try:
            try:
                resp = _post_patiently(body, key)
            except RuntimeError as e:          # an option this model doesn't take: retry with the basics
                if "HTTP 400" not in str(e) or "API key" in str(e):
                    raise
                log.info("%s rejected an option (%s); retrying without thinking settings", model, str(e)[:120])
                body.pop("generation_config", None)
                resp = _post_patiently(body, key)
            return parse_json(response_text(resp)), model
        except QuotaError as e:
            log.warning("%s is out of free quota for now: %s", model, str(e)[:160])
            _exhausted[model] = time.time()
            errors.append(f"{model}: out of free quota")
        except Exception as e:
            if "HTTP 503" in str(e):
                _busy[model] = time.time()
            errors.append(f"{model}: {type(e).__name__}: {str(e)[:160]}")
            if "API key" in str(e) or "HTTP 401" in str(e) or "HTTP 403" in str(e):
                break
    raise RuntimeError("; ".join(errors) or "every model is out of free quota for now")


# ----------------------------------------------------------------------------- prompt + conversion
def build_prompt(ctx: dict) -> str:
    from studio.director import transcript_text
    c = ctx["campaign"]
    loud = sorted(ctx.get("loudness") or [], key=lambda x: -x[1])[:6]
    info = {
        "campaign": c.get("name"), "brief": c.get("brief", ""), "required_text": c.get("required_text", []),
        "banned": c.get("banned", ""), "min_seconds": c.get("min_seconds"), "max_seconds": c.get("max_seconds"),
        "target_seconds": c.get("target_seconds"), "streamer": ctx.get("channel", "").split(":")[-1],
        "streamer_pronoun": c.get("pronoun") or "they", "hook_enabled": c.get("hook", True),
        "clip_seconds": ctx["duration"], "chat_spike_at": ctx["moment"],
        "chat_spammed": (ctx.get("signals") or {}).get("top_chat") or [],
        "viewer_made_clips_here": ctx.get("viewer_clips", 0),
        "loudest_seconds": [[round(t, 1), round(l, 1)] for t, l in sorted(loud)],
        "webcam_found": (ctx.get("facecam") or {}).get("kind") == "webcam",
        "recent_caption_styles_to_avoid": ctx.get("recent_styles", [])[-3:],
        "recent_hooks_to_avoid": ctx.get("recent_hooks", [])[-10:],
    }
    parts = ["CLIP INFO\n" + json.dumps(info, ensure_ascii=False, indent=1),
             "TRANSCRIPT (speech-to-text of everything audible, streamer AND game; [t] = seconds)\n"
             + (transcript_text(ctx.get("words") or []) or "(nothing said)")]
    if ctx.get("notes"):
        prev = ctx.get("previous_plan") or {}
        parts.append(f"RE-EDIT. The user reviewed your last edit and asks: {ctx['notes']!r}\n"
                     f"Your last edit: hook {((prev.get('hook') or {}).get('text'))!r}, keep {prev.get('segments') or prev.get('keep')}, "
                     f"zooms {prev.get('zooms')}. Change what they ask; keep what they didn't mention.")
    return "\n\n".join(parts)


def to_raw(d: dict, model: str, ctx: dict) -> dict:
    """The model's decision -> the raw plan format director.validate() takes."""
    keep = [{"from": float(k["from"]), "to": float(k["to"]), "why": str(k.get("why", ""))}
            for k in d.get("keep") or [] if float(k.get("to", 0)) > float(k.get("from", 0))]
    if not keep:
        keep = [{"from": 0.0, "to": ctx["duration"], "why": "whole clip"}]
    verdict, story, hook = d.get("verdict") or {}, d.get("story") or {}, d.get("hook") or {}
    zooms = [{"at": float(z["at"]), "strength": float(z.get("strength", 0.14)), "target": z.get("target", "face"),
              "why": str(z.get("why", ""))} for z in d.get("zooms") or [] if str(z.get("why", "")).strip()]
    return {
        "keep_start": keep[0]["from"], "keep_end": keep[-1]["to"], "_segments": keep,
        "layout": d.get("layout"), "game_x": d.get("game_x", 0.5),
        "webcam_box": {"present": False, "x": 0, "y": 0, "w": 0, "h": 0},
        "hook_text": str(hook.get("text") or "").upper(), "hook_seconds": 2.5,
        "caption_style": d.get("caption_style"), "emphasis_words": d.get("emphasis_words") or [],
        "zooms": zooms, "sfx": [], "logo_corner": "none",
        "post_title": "", "post_caption": d.get("post_caption", ""), "hashtags": [],
        "why": f"{story.get('beats', '')} | {verdict.get('reason', '')}".strip(" |"),
        "quality": verdict.get("score", 5), "skip": not verdict.get("post", True),
        "skip_reason": verdict.get("reason", "") if not verdict.get("post", True) else "",
        "_speech": d.get("speech") or [], "_watched": d.get("watched") or {},
        "_story": story, "_hook_why": hook.get("why", ""), "_layout_why": d.get("layout_why", ""),
        "_source": f"gemini:{model}", "_kind": None,
    }


def plan_raw(ctx: dict, clip_path, key: str) -> dict:
    video = proxy(Path(clip_path))
    decision, model = ask(key, video, build_prompt(ctx))
    log.info("%s: %s", model, (decision.get("verdict") or {}).get("reason", "")[:160])
    return to_raw(decision, model, ctx)
