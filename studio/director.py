"""The director: decides how each clip is edited.

It reads the campaign brief and rules, the word-timed transcript, how chat reacted, where
the streamer's webcam is, and looks at frames from the clip. It returns an edit plan:
what to keep, layout, hook title, caption style, words to emphasise, zoom punch-ins,
sound hits, logo, and the post title/caption/hashtags.

By default the free built-in director (studio/free_director.py) decides: no AI, no cost.
Switch Settings -> Director to "Claude (paid)" with an Anthropic API key to have Claude plan
each edit instead; if that call fails, the free director takes over so editing never stops.
`validate()` runs on every plan either way and enforces the campaign's hard rules.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import subprocess

from studio.editor.captions import PRESETS
from studio.editor.render import SFX
from studio.editor.transcribe import Word

log = logging.getLogger("director")

LAYOUTS = ["facecam_split", "face_crop", "blur_fit", "game_only"]
CORNERS = ["top_left", "top_right", "bottom_left", "bottom_right", "none"]
STYLES = list(PRESETS)
SOUNDS = sorted(SFX) or ["boom", "whoosh", "pop", "riser", "ding", "bass_drop"]
DEFAULT_MODEL = "claude-opus-5-5"

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keep_start", "keep_end", "layout", "game_x", "webcam_box", "hook_text", "hook_seconds",
                 "caption_style", "emphasis_words", "zooms", "sfx", "logo_corner", "post_title", "post_caption",
                 "hashtags", "why", "quality", "skip", "skip_reason"],
    "properties": {
        "keep_start": {"type": "number", "description": "seconds into the source clip where the edit starts"},
        "keep_end": {"type": "number", "description": "seconds into the source clip where the edit ends"},
        "layout": {"type": "string", "enum": LAYOUTS},
        "game_x": {"type": "number", "description": "0..1 horizontal focus of the gameplay crop (0.5 = centre)"},
        "webcam_box": {
            "type": "object", "additionalProperties": False, "required": ["present", "x", "y", "w", "h"],
            "properties": {"present": {"type": "boolean"}, "x": {"type": "number"}, "y": {"type": "number"},
                           "w": {"type": "number"}, "h": {"type": "number"}},
            "description": "the streamer's webcam overlay as fractions of the frame (0..1), if one is visible",
        },
        "hook_text": {"type": "string", "description": "title shown at the top for the first seconds; 2-7 words; '' for none"},
        "hook_seconds": {"type": "number"},
        "caption_style": {"type": "string", "enum": STYLES},
        "emphasis_words": {"type": "array", "items": {"type": "string"}},
        "zooms": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["at", "strength"],
            "properties": {"at": {"type": "number"}, "strength": {"type": "number"}}}},
        "sfx": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["at", "sound"],
            "properties": {"at": {"type": "number"}, "sound": {"type": "string", "enum": SOUNDS}}}},
        "logo_corner": {"type": "string", "enum": CORNERS},
        "post_title": {"type": "string"},
        "post_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string", "description": "one sentence: why these editing choices fit this clip"},
        "quality": {"type": "integer", "description": "1-10: how likely this clip is to do well"},
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
    },
}

SYSTEM = """You are the lead editor of a short-form clipping channel. Streams are monitored automatically; when chat and audio spike, a ~1 minute clip is captured. Your job: turn each captured clip into a vertical (9:16) Short / TikTok / Reel that people watch to the end, for a paid clipping campaign.

Priorities, in order:
1. The campaign brief's requirements are hard rules (required text, hashtags, tags, logo, tone, banned content, length). Never break them.
2. Start strong: begin at most ~1 second before the first interesting line or action. Cut dead air at the start. End right after the reaction lands; don't trail off.
3. Make it feel edited, not just cropped, but don't overdo it. Pick what THIS clip needs:
   - layout: "facecam_split" (webcam on top, gameplay below) when a webcam overlay is visible and the streamer's reaction matters; "face_crop" for just-chatting/IRL where the person fills the frame; "game_only" when the action is in the middle of the screen and the webcam doesn't matter; "blur_fit" when the whole wide frame must stay visible.
   - hook_text: a short, punchy title (2-7 words) that creates curiosity about what's coming, written like a viral Short. True to the clip: no lies, no fake drama. Use the language the campaign asks for; otherwise the language spoken in the clip. No emoji (they're added to the post caption instead). Leave empty if a hook would hurt.
   - caption_style: vary between clips. Avoid repeating the styles listed as recently used, unless the brief asks for one look.
   - emphasis_words: 1-4 words from the transcript that carry the punchline or emotion.
   - zooms: 0-3 punch-ins exactly when the reaction or key moment hits (strength 0.08-0.22).
   - sfx: 0-3 sounds that support a beat (e.g. "boom"/"bass_drop" on the payoff, "whoosh" on a cut-in, "pop" on a reveal, "riser" before a payoff, "ding" on a correct/clean moment). Silence is fine when the audio already hits.
4. Post text: post_title (short), post_caption (one or two lines in the creator-friendly voice of the campaign, may include emoji), hashtags (include every hashtag the brief requires).
5. Be honest in quality (1-10). Set skip=true with a reason if the clip is unusable (nothing happens, breaks the brief, offensive, mostly ads/menus).

All times are seconds in the SOURCE clip's timeline. The detected moment (when chat exploded, already shifted back for chat delay) is given; the payoff is usually close to it. The webcam_box is your read of where the streamer's webcam overlay is in the frames (fractions 0..1 of the full frame); set present=false if there is none."""


# --------------------------------------------------------------------------- context
def frame_jpegs(path, times: list[float], width: int = 512) -> list[bytes]:
    out = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "-"],
                           capture_output=True)
        if r.stdout:
            out.append(r.stdout)
    return out


def transcript_text(words: list[Word]) -> str:
    """Compact word-timed transcript: '[3.2] no way [3.9] he actually ...' every few words."""
    parts, last = [], -10.0
    for w in words:
        if w.start - last >= 1.5:
            parts.append(f"[{w.start:.1f}]")
            last = w.start
        parts.append(w.text)
    return " ".join(parts)


def build_context(clip: dict, campaign: dict, words: list[Word], facecam: dict | None, duration: float,
                  recent_styles: list[str] | None = None, notes: str = "", previous_plan: dict | None = None,
                  loudness: list | None = None, language: str | None = None, recent_hooks: list[str] | None = None) -> dict:
    m = clip.get("moment_offset")
    moment = float(m) if m is not None else duration / 2
    return {
        "duration": round(duration, 2), "moment": round(moment, 2), "channel": clip.get("channel", ""),
        "signals": clip.get("signals") or {}, "words": words, "facecam": facecam or {"kind": "none"},
        "recent_styles": recent_styles or [], "notes": notes, "previous_plan": previous_plan,
        "campaign": campaign, "loudness": loudness or [], "language": language, "recent_hooks": recent_hooks or [],
        "viewer_clips": clip.get("viewer_clips", 0), "scout": clip.get("scout"),
    }


# --------------------------------------------------------------------------- AI director
def _user_content(ctx: dict, frames: list[bytes], frame_times: list[float]) -> list[dict]:
    c = ctx["campaign"]
    brief = {
        "campaign": c.get("name"), "brief": c.get("brief", ""), "min_seconds": c.get("min_seconds"),
        "max_seconds": c.get("max_seconds"), "required_text": c.get("required_text", []),
        "cta": c.get("cta", ""), "banned": c.get("banned", ""), "has_logo": bool(c.get("logo_path")),
        "preferred_layout": c.get("layout") or "director decides",
        "preferred_caption_style": c.get("caption_style") or "director decides (vary)",
        "hook_enabled": c.get("hook", True),
        "streamer_pronoun": c.get("pronoun") or "they",
    }
    fc = ctx["facecam"]
    info = {
        "clip_seconds": ctx["duration"], "detected_moment_at": ctx["moment"], "channel": ctx["channel"],
        "chat_signals": ctx["signals"], "auto_webcam_detection": {"kind": fc.get("kind"), "cam": fc.get("cam")},
        "recently_used_caption_styles": ctx["recent_styles"][-4:],
        "available_sounds": SOUNDS, "available_caption_styles": STYLES,
    }
    content: list[dict] = [{"type": "text", "text": "CAMPAIGN\n" + json.dumps(brief, ensure_ascii=False, indent=1)}]
    for jpg, t in zip(frames, frame_times):
        content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpg).decode()}})
    content.append({"type": "text", "text": "CLIP\n" + json.dumps(info, ensure_ascii=False, indent=1)
                    + "\n\nTRANSCRIPT (word timings in brackets)\n" + (transcript_text(ctx["words"]) or "(no speech)")})
    if ctx.get("notes"):
        content.append({"type": "text", "text": "The human editor reviewed your previous edit and asks for changes. "
                        "Apply these notes:\n" + ctx["notes"] + "\n\nPrevious plan:\n" + json.dumps(ctx.get("previous_plan") or {})})
    content.append({"type": "text", "text": "Return the edit plan."})
    return content


def ai_plan(ctx: dict, clip_path, api_key: str, model: str = DEFAULT_MODEL, effort: str = "medium") -> dict:
    """Ask Claude for the plan. Raises on any failure (the caller falls back to rules)."""
    import anthropic
    times = sorted({round(max(0.5, min(ctx["duration"] - 0.5, t)), 1)
                    for t in (ctx["moment"] - 4, ctx["moment"], ctx["moment"] + 2.5, ctx["moment"] + 6)})
    frames = frame_jpegs(clip_path, times)
    client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=180)
    resp = client.beta.messages.create(
        model=model, max_tokens=8000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",   # re-run on another model if one declines
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        messages=[{"role": "user", "content": _user_content(ctx, frames, times)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined this clip")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("plan was cut off (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    raw = json.loads(text)
    raw["_source"] = f"ai:{resp.model}"
    raw["_usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return raw


# --------------------------------------------------------------------------- rule-based director
GENERIC_HOOKS = ["WAIT FOR IT", "CHAT LOST IT", "NOBODY SAW THIS COMING", "THIS WENT WRONG FAST",
                 "NOBODY EXPECTED THIS", "WATCH TILL THE END", "THE REACTION THOUGH"]


def apply_notes(plan: dict, notes: str, ctx: dict) -> dict:
    """Rule-based handling of common re-edit requests (the AI director understands anything)."""
    n = notes.lower()
    if not n:
        return plan
    p = dict(plan)
    if re.search(r"no (sound|sfx|effects?)|without sound|remove sound", n):
        p["sfx"] = []
    if re.search(r"no zoom|without zoom|remove zoom", n):
        p["zooms"] = []
    if re.search(r"no hook|without (a )?hook|remove (the )?hook", n):
        p["hook_text"] = ""
    for word, layout in (("crop", "face_crop"), ("facecam", "facecam_split"), ("blur", "blur_fit"), ("game only", "game_only")):
        if word in n:
            p["layout"] = layout
    for name in PRESETS:
        if name.split("_")[0] in n or name.replace("_", " ") in n:
            p["caption_style"] = name
    m = re.search(r"start (\d+(?:\.\d+)?)\s*s(?:ec\w*)? later", n)
    if m:
        p["keep_start"] = p["keep_start"] + float(m.group(1))
    m = re.search(r"(?:end|stop) (\d+(?:\.\d+)?)\s*s(?:ec\w*)? (?:earlier|sooner)", n)
    if m:
        p["keep_end"] = p["keep_end"] - float(m.group(1))
    if "shorter" in n:
        p["keep_end"] = p["keep_start"] + (p["keep_end"] - p["keep_start"]) * 0.75
    if "longer" in n:
        p["keep_start"], p["keep_end"] = max(0.0, p["keep_start"] - 4), min(ctx["duration"], p["keep_end"] + 4)
    m = re.search(r"hook[^\"“]*[\"“]([^\"”]+)[\"”]", notes)
    if m:
        p["hook_text"] = m.group(1)
    elif "hook" in n and not re.search(r"no hook|remove", n):
        p["hook_text"] = random.choice([h for h in GENERIC_HOOKS if h != plan.get("hook_text")])
    p["why"] = f"Re-edited with your notes: {notes.strip()}"
    return p


def rule_plan(ctx: dict, seed: int | None = None) -> dict:
    """The free built-in director (studio/free_director.py)."""
    from studio import free_director
    return free_director.rule_plan(ctx, seed)


# --------------------------------------------------------------------------- validation
def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def validate(raw: dict, ctx: dict) -> dict:
    """Clamp everything into range, enforce campaign rules, convert to the render plan format."""
    c, dur = ctx["campaign"], ctx["duration"]
    lo, hi = float(c.get("min_seconds") or 0), float(c.get("max_seconds") or dur)
    start = min(max(_num(raw.get("keep_start"), 0), 0), max(dur - 1, 0))
    end = min(max(_num(raw.get("keep_end"), dur), start + 1), dur)
    if end - start > hi:
        end = start + hi
    if end - start < lo:   # grow toward the end first, then the start
        end = min(dur, start + lo)
        start = max(0.0, end - lo)
    segments = clean_segments(raw.get("_segments"), dur, lo, hi) if raw.get("_segments") else [[round(start, 2), round(end, 2)]]
    start, end = segments[0][0], segments[-1][1]

    fc = dict(ctx["facecam"] or {})
    box = raw.get("webcam_box") or {}
    if box.get("present") and not fc.get("cam") and 0.03 < _num(box.get("w"), 0) < 0.6 and 0.03 < _num(box.get("h"), 0) < 0.7:
        x, y, w, h = (min(max(_num(box.get(k), 0), 0), 1) for k in "xywh")
        fc = {"kind": "webcam", "cam": [x, y, min(w, 1 - x), min(h, 1 - y)], "source": "ai"}
    layout = c.get("layout") or raw.get("layout")
    if layout not in LAYOUTS:
        layout = "blur_fit"
    if layout == "facecam_split" and not fc.get("cam"):
        layout = "face_crop" if fc.get("kind") == "center_face" else "blur_fit"

    def inside(t: float) -> bool:
        return any(a + 0.2 <= t <= b - 0.3 for a, b in segments)

    zooms = []
    for z in raw.get("zooms") or []:
        at = _num(z.get("at"), -1)
        if inside(at) and all(abs(at - o["at"]) >= 3 for o in zooms):     # punch-ins need room to breathe
            item = {"at": round(at, 2), "strength": round(min(max(_num(z.get("strength"), 0.14), 0.06), 0.25), 3)}
            if z.get("target") in ("face", "game"):
                item["target"] = z["target"]
            if z.get("why"):
                item["why"] = str(z["why"])[:200]
            zooms.append(item)
    zooms = zooms[:3]
    sfx = [{"at": round(_num(s.get("at"), -1), 2), "sound": s.get("sound")}
           for s in raw.get("sfx") or [] if s.get("sound") in SFX and start <= _num(s.get("at"), -1) <= end - 0.2][:4]

    hook_text = re.sub(r"\s+", " ", str(raw.get("hook_text") or "")).strip()[:48] if c.get("hook", True) else ""
    style = c.get("caption_style") or raw.get("caption_style")
    if style not in PRESETS:
        style = STYLES[0]
    corner = raw.get("logo_corner")
    logo = None
    if c.get("logo_path"):
        corner = c.get("logo_corner") or (corner if corner in CORNERS and corner != "none" else "top_right")
        logo = {"corner": corner, "scale": float(c.get("logo_scale") or 0.16)}

    tags = [t if t.startswith(("#", "@")) else f"#{t}" for t in (raw.get("hashtags") or []) if t.strip()]
    for req in c.get("required_text") or []:     # campaign requirements always make it in
        if req not in tags and req not in (raw.get("post_caption") or ""):
            tags.append(req)
    q = int(min(max(_num(raw.get("quality"), 5), 1), 10))
    return {
        "keep": [round(start, 2), round(end, 2)], "layout": layout, "game_x": min(max(_num(raw.get("game_x"), 0.5), 0), 1),
        "facecam": fc, "hook": {"text": hook_text, "seconds": min(max(_num(raw.get("hook_seconds"), 2.5), 1.0), 4.0),
                                "box_color": c.get("brand_color") or "#E8202A"} if hook_text else None,
        "caption_style": style, "emphasis_words": [str(w) for w in raw.get("emphasis_words") or []][:4],
        "zooms": zooms, "sfx": sfx, "logo": logo,
        "post": {"title": str(raw.get("post_title") or " ".join(w[:1].upper() + w[1:].lower() for w in hook_text.split()))[:100],
                 "caption": str(raw.get("post_caption") or ""), "hashtags": tags},
        "why": str(raw.get("why") or ""), "quality": q,
        "skip": bool(raw.get("skip")), "skip_reason": str(raw.get("skip_reason") or ""),
        "source": raw.get("_source", "ai"), "usage": raw.get("_usage"), "kind": raw.get("_kind"),
        "segments": segments, "speech": [s for s in raw.get("_speech") or [] if isinstance(s, dict)],
        "watched": raw.get("_watched") or None, "story": raw.get("_story") or None,
        "hook_why": raw.get("_hook_why") or "", "layout_why": raw.get("_layout_why") or "",
    }


def clean_segments(segs, dur: float, lo: float, hi: float) -> list[list[float]]:
    """Kept parts: inside the clip, in order, no overlaps, no slivers, total within the campaign's length."""
    out: list[list[float]] = []
    for s in sorted(((min(max(_num(x.get("from"), 0), 0), dur), min(max(_num(x.get("to"), 0), 0), dur))
                     for x in segs if isinstance(x, dict)), key=lambda p: p[0]):
        a, b = s
        if b - a < 0.6:
            continue
        if out and a - out[-1][1] < 0.4:          # a gap this small isn't worth a cut
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    if not out:
        return [[0.0, min(dur, hi or dur)]]
    total = sum(b - a for a, b in out)
    while hi and total > hi:                       # too long: shorten from the end
        a, b = out[-1]
        cut = min(total - hi, b - a)
        if cut >= b - a - 0.6 and len(out) > 1:
            out.pop()
        else:
            out[-1][1] = b - cut
        total = sum(b - a for a, b in out)
    if lo and total < lo:                          # too short: let the last part run on
        out[-1][1] = min(dur, out[-1][1] + (lo - total))
    return [[round(a, 2), round(b, 2)] for a, b in out]


def streamer_words(words: list[Word], plan: dict) -> list[Word]:
    """Captions show what the streamer says, not game characters (the director marks who talks when)."""
    other = [(float(s["from"]), float(s["to"])) for s in plan.get("speech") or []
             if s.get("who") in ("game", "other") and "from" in s and "to" in s]
    if not other:
        return words
    return [w for w in words if not any(a <= (w.start + w.end) / 2 <= b for a, b in other)]


def plan(ctx: dict, clip_path, settings: dict) -> dict:
    """AI plan when a key is set, rules otherwise or on failure. Always validated."""
    settings = settings or {}
    key = settings.get("anthropic_api_key") or ""
    raw, note = None, ""
    if settings.get("director") == "gemini":
        if not settings.get("gemini_api_key"):
            note = "Gemini director picked but no key in Settings; used the free director."
        else:
            from studio import gemini_director
            try:
                raw = gemini_director.plan_raw(ctx, clip_path, settings["gemini_api_key"])
            except gemini_director.DirectorWait:
                raise          # temporary: the worker re-queues the clip instead of using the rules director
            except Exception as e:
                note = f"Gemini director unavailable ({type(e).__name__}: {str(e)[:200]}); used the free director."
                log.warning(note)
    elif key and settings.get("director", "free") == "ai":
        try:
            raw = ai_plan(ctx, clip_path, key, settings.get("model") or DEFAULT_MODEL, settings.get("effort") or "medium")
        except Exception as e:
            note = f"AI director unavailable ({type(e).__name__}: {str(e)[:160]}); used the free director."
            log.warning(note)
    if raw is None:
        prev = ctx.get("previous_plan")
        if ctx.get("notes") and prev:      # re-edit: start from the last plan, apply the notes
            base = {"keep_start": prev["keep"][0], "keep_end": prev["keep"][1], "layout": prev["layout"],
                    "game_x": prev.get("game_x", 0.5), "hook_text": (prev.get("hook") or {}).get("text", ""),
                    "hook_seconds": (prev.get("hook") or {}).get("seconds", 2.5), "caption_style": prev["caption_style"],
                    "emphasis_words": prev.get("emphasis_words", []), "zooms": prev.get("zooms", []), "sfx": prev.get("sfx", []),
                    "logo_corner": (prev.get("logo") or {}).get("corner", "none"), "post_title": prev["post"].get("title"),
                    "post_caption": prev["post"].get("caption"), "hashtags": prev["post"].get("hashtags", []),
                    "quality": prev.get("quality", 5), "skip": False, "skip_reason": "", "_source": "free",
                    "webcam_box": {"present": False, "x": 0, "y": 0, "w": 0, "h": 0}}
            raw = apply_notes(base, ctx["notes"], ctx)
        else:
            raw = rule_plan(ctx)
    p = validate(raw, ctx)
    if note:
        p["why"] = (note + " " + p["why"]).strip()
    return p
