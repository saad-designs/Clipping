"""Audio loudness per second for one recorded segment.

Uses ffmpeg's ebur128 filter (the broadcast loudness standard, in LUFS) on the audio
track only, so it's cheap: ~50ms of CPU for a 10s segment.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_LINE = re.compile(r"\] t:\s*([\d.]+)\s.*?M:\s*(-?[\d.]+)")
SILENCE = -70.0   # ebur128 reports -120 for digital silence; clamp so it doesn't wreck averages


def parse_ebur128(output: str, seg_start: float) -> list[tuple[float, float]]:
    """ffmpeg stderr -> [(unix_second, peak momentary LUFS in that second)]."""
    peaks: dict[int, float] = {}
    for m in _LINE.finditer(output):
        t, lufs = float(m.group(1)), max(float(m.group(2)), SILENCE)
        sec = int(seg_start + t)
        if lufs > peaks.get(sec, -999):
            peaks[sec] = lufs
    return sorted(peaks.items())


def analyze(seg_path: Path, seg_start: float) -> list[tuple[float, float]]:
    proc = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-v", "verbose", "-i", str(seg_path),
         "-map", "0:a:0?", "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    return parse_ebur128(proc.stderr, seg_start)
