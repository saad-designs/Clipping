"""Word-by-word captions as an ASS subtitle file (burned in by ffmpeg/libass).

A few words at a time, big bold text with a thick outline, the spoken word highlighted:
the style short-form viewers expect.
"""
from __future__ import annotations

import re

from studio.editor.transcribe import Word


def _ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()   # ASS colours are BGR


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def chunk(words: list[Word], per_line: int) -> list[list[Word]]:
    """Group words into short lines, breaking early on pauses and sentence ends."""
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per_line or w.start - cur[-1].end > 0.6 or re.search(r"[.!?]$", cur[-1].text)):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def build_ass(words: list[Word], clip_start: float, clip_end: float, style: dict,
              width: int = 1080, height: int = 1920) -> str:
    """`words` use source-clip time; output times are relative to `clip_start`."""
    words = [Word(w.start - clip_start, w.end - clip_start, w.text) for w in words
             if w.end > clip_start and w.start < clip_end]
    hi = _ass_color(style.get("highlight_color", "#FFD21F")) + "&"   # inline override form: &H00BBGGRR&
    margin_v = int(height * (1 - float(style.get("position", 0.7))))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.get("font", "Arial")},{int(style.get("size", 96))},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    upper = style.get("uppercase", True)
    length = clip_end - clip_start
    groups = chunk(words, int(style.get("words_per_line", 3)))
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else length
        texts = [_clean(w.text.upper() if upper else w.text) for w in group]
        for i, w in enumerate(group):
            start = w.start
            # last word lingers a moment, but never overlaps the next line
            end = group[i + 1].start if i + 1 < len(group) else min(w.end + 0.25, next_start)
            start, end = max(0.0, start), min(length, end)
            if end <= start:
                continue
            shown = " ".join(f"{{\\c{hi}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t for j, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{shown}")
    return header + "\n".join(lines) + "\n"
