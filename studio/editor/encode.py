"""Pick the fastest H.264 encoder this laptop has, once, and remember it."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("encode")

CANDIDATES = ["h264_nvenc", "h264_qsv", "h264_videotoolbox", "h264_amf", "libx264"]

ARGS = {
    "h264_nvenc": ["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "12M", "-maxrate", "16M"],
    "h264_qsv": ["-global_quality", "21", "-look_ahead", "0"],
    "h264_videotoolbox": ["-b:v", "12M", "-allow_sw", "1"],
    "h264_amf": ["-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"],
    "libx264": ["-preset", "medium", "-crf", "20"],
}
CACHE = Path.home() / ".clipping_studio" / "encoder.json"


def _works(enc: str) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=black:s=1080x1920:d=0.3", "-c:v", enc, "-f", "null", "-"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick(forced: str = "auto") -> str:
    if forced and forced != "auto":
        return forced
    try:
        return json.loads(CACHE.read_text())["encoder"]
    except (OSError, ValueError, KeyError):
        pass
    enc = next((e for e in CANDIDATES if _works(e)), "libx264")
    log.info("video encoder: %s", enc)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"encoder": enc}))
    except OSError:
        pass
    return enc


def video_args(enc: str) -> list[str]:
    return ["-c:v", enc, *ARGS.get(enc, []), "-profile:v", "high"]

