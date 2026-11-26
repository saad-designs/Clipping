"""inbox/<clip>.mp4 (+ .json sidecar) → ready/<clip>.mp4 + caption text + meta, upload-ready."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from studio.editor import captions, encode, reframe, transcribe, trim

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


def edit(src: Path, cfg: dict, layout: str | None = None) -> Path:
    """Edit one inbox clip. Returns the finished mp4 path."""
    side_path = src.with_suffix(".json")
    meta = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else {}
    rules = meta.get("rules") or {}
    ed, cap = cfg["editing"], cfg["captions"]
    duration, has_audio = probe(src)

    words = transcribe.transcribe(src, cap["whisper_model"], cap.get("language")) \
        if cap.get("enabled", True) and has_audio else []
    start, end = trim.choose_window(duration, meta.get("moment_offset"), rules.get("min_seconds"),
                                    rules.get("max_seconds"), float(ed["target_seconds"]), words)

    layout = layout or rules.get("layout") or ed["layout"]
    fc = cfg.get("facecam", {}).get(meta.get("channel", ""))
    if layout == "facecam" and not fc:
        log.warning("no facecam rectangle set for %s in studio.yaml; using blur layout", meta.get("channel"))
        layout = "blur"
    graph = reframe.build(layout, int(ed["fps"]), float(ed.get("blur_zoom", 1.25)), fc)

    ready_dir = src.parent.parent / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    out = ready_dir / src.name
    enc = encode.pick(ed.get("encoder", "auto"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if words:
            (tmp / "captions.ass").write_text(captions.build_ass(words, start, end, cap), encoding="utf-8")
            # run ffmpeg inside tmp and use a bare file name: avoids Windows drive-letter escaping in filters
            graph = graph.replace("[v]", "[pre];[pre]ass=captions.ass[v]")
        if has_audio:
            graph += ";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
               "-i", str(src.resolve()), "-filter_complex", graph, "-map", "[v]"]
        if has_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        cmd += [*encode.video_args(enc), "-movflags", "+faststart", str(tmp / "out.mp4")]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0 and enc != "libx264":
            log.warning("%s failed, retrying with libx264: %s", enc, r.stderr[-300:])
            i = cmd.index("-c:v")
            cmd[i:i + len(encode.video_args(enc))] = encode.video_args("libx264")
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr[-800:]}")
        shutil.move(str(tmp / "out.mp4"), out)

    hook = hook_line(words, start, end)
    required = " ".join(rules.get("required_text") or [])
    streamer = meta.get("channel", "").split(":")[-1]
    caption_txt = "\n\n".join(x for x in [hook, f"🎥 {streamer}" if streamer else "", required] if x)
    out.with_suffix(".caption.txt").write_text(caption_txt + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps({
        **meta, "kept": [start, end], "length": round(end - start, 2), "layout": layout,
        "captions": bool(words), "encoder": enc, "transcript": " ".join(w.text for w in words),
    }, indent=2), encoding="utf-8")

    done = src.parent / "done"
    done.mkdir(exist_ok=True)
    shutil.move(str(src), done / src.name)
    if side_path.exists():
        shutil.move(str(side_path), done / side_path.name)
    log.info("✅ ready: %s  (%.0fs, %s, captions %s)", out, end - start, layout, "on" if words else "off")
    return out


def pending(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.glob("*/inbox/*.mp4"))
