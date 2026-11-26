"""inbox/<clip>.mp4 (+ .json sidecar) → ready/<clip>.mp4 + caption text + meta, upload-ready."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from studio.editor import transcribe

log = logging.getLogger("editor")

HOOK_WORDS = 4


def probe(path: Path) -> tuple[float, bool]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return float(data["format"]["duration"]), any(s["codec_type"] == "audio" for s in data["streams"])


def _shorten(text: str, limit: int = 120) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"   # never cut a word in half


def hook_line(words: list[transcribe.Word], start: float, end: float) -> str:
    """First complete sentence in the kept part, as a starting point for the post caption."""
    text = " ".join(w.text for w in words if start <= w.start < end)
    for sentence in text.replace("?", "?.").replace("!", "!.").split("."):
        s = sentence.strip()
        if len(s.split()) >= HOOK_WORDS:
            return _shorten(s)
    return _shorten(text)


LAYOUT_ALIASES = {"blur": "blur_fit", "crop": "game_only", "facecam": "facecam_split"}


def edit(src: Path, cfg: dict, layout: str | None = None) -> Path:
    """Edit one inbox clip (command-line path; the dashboard uses studio.worker). Returns the mp4."""
    import os

    from studio import director
    from studio.editor import facecam as facecam_mod
    from studio.editor import render

    side_path = src.with_suffix(".json")
    meta = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else {}
    rules = meta.get("rules") or {}
    ed, cap = cfg["editing"], cfg["captions"]
    duration, has_audio = probe(src)
    words = transcribe.transcribe(src, cap["whisper_model"], cap.get("language")) \
        if cap.get("enabled", True) and has_audio else []

    channel = meta.get("channel", "")
    manual = cfg.get("facecam", {}).get(channel)
    fc = {"kind": "webcam", "cam": manual["cam"], "source": "manual"} if manual and manual.get("cam") \
        else facecam_mod.detect(src).to_dict()
    layout = layout or rules.get("layout") or ed.get("layout")
    campaign = {"name": meta.get("campaign", ""), "min_seconds": rules.get("min_seconds"),
                "max_seconds": rules.get("max_seconds"), "target_seconds": ed.get("target_seconds", 32),
                "required_text": rules.get("required_text") or [],
                "layout": LAYOUT_ALIASES.get(layout, layout) if layout and layout != "auto" else None}
    ctx = director.build_context({**meta, "channel": channel}, campaign, words, fc, duration)
    plan = director.plan(ctx, src, {"anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
                                    "director": "ai", "model": cfg.get("model"), "effort": cfg.get("effort")})

    ready_dir = src.parent.parent / "ready"
    out = ready_dir / src.name
    render.render(src, plan, out, campaign, words, plan.get("facecam") or fc, cap, ed.get("encoder", "auto"),
                  int(ed.get("fps", 30)))
    post = plan["post"]
    text = "\n\n".join(x for x in [post.get("title", ""), post.get("caption", ""), " ".join(post.get("hashtags") or [])] if x)
    out.with_suffix(".caption.txt").write_text(text + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps({
        **meta, "kept": plan["keep"], "length": round(plan["keep"][1] - plan["keep"][0], 2), "layout": plan["layout"],
        "captions": bool(words), "plan": plan, "transcript": " ".join(w.text for w in words),
    }, indent=2, default=str), encoding="utf-8")

    done = src.parent / "done"
    done.mkdir(exist_ok=True)
    shutil.move(str(src), done / src.name)
    if side_path.exists():
        shutil.move(str(side_path), done / side_path.name)
    log.info("✅ ready: %s  (%.0fs, %s)", out, plan["keep"][1] - plan["keep"][0], plan["layout"])
    return out


def pending(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.glob("*/inbox/*.mp4"))
