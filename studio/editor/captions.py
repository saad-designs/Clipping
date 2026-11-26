"""Burned-in captions (ASS subtitles rendered by ffmpeg/libass).

Short-form style: a few big bold words at a time, the spoken word pops (quick scale-up)
and changes colour, emphasis words stay in the accent colour, and an optional hook title
sits at the top for the first seconds. Four presets so clips don't all look the same.
"""
from __future__ import annotations

import re
from pathlib import Path

from studio.editor.transcribe import Word

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
DEFAULT_FONT = "Inter Black"          # bundled (assets/fonts, OFL licence)

# colours are "#RRGGBB"; alpha for boxes is 00 (opaque) .. FF (invisible)
PRESETS: dict[str, dict] = {
    "pop_yellow":    {"text": "#FFFFFF", "active": "#FFD21F", "emph": "#FFD21F", "outline": "#000000",
                      "outline_w": 9, "shadow": 3, "box": False, "pop": 1.18, "size": 1.0},
    "karaoke_box":   {"text": "#FFFFFF", "active": "#39FF6A", "emph": "#39FF6A", "outline": "#000000",
                      "outline_w": 14, "shadow": 0, "box": True, "box_alpha": "40", "pop": 1.10, "size": 0.92},
    "outline_white": {"text": "#FFFFFF", "active": "#4DE3FF", "emph": "#4DE3FF", "outline": "#101010",
                      "outline_w": 12, "shadow": 0, "box": False, "pop": 1.25, "size": 1.08},
    "punch_red":     {"text": "#FFFFFF", "active": "#FF3B3B", "emph": "#FF3B3B", "outline": "#000000",
                      "outline_w": 8, "shadow": 6, "box": False, "pop": 1.3, "size": 1.0},
}
DEFAULT_PRESET = "pop_yellow"


def _ass_color(hex_rgb: str, alpha: str = "00") -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()   # ASS colours are AABBGGRR


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")


def _clean(text: str) -> str:
    # emoji don't render reliably in burned-in text (outline glyphs or empty boxes): post captions keep them
    text = _EMOJI.sub("", text)
    return re.sub(r"\s{2,}", " ", text.replace("\\", "").replace("{", "(").replace("}", ")")).strip()


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def chunk(words: list[Word], per_line: int, max_chars: int = 16) -> list[list[Word]]:
    """Group words into short lines, breaking early on pauses, sentence ends, and when the line
    would get too wide (long words, e.g. German, would otherwise wrap into a 3-line block)."""
    out, cur = [], []
    for w in words:
        width = len(" ".join(x.text for x in cur + [w]))
        if cur and (len(cur) >= per_line or width > max_chars or w.start - cur[-1].end > 0.6
                    or re.search(r"[.!?]$", cur[-1].text)):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def build_ass(words: list[Word], clip_start: float, clip_end: float, style: dict,
              width: int = 1080, height: int = 1920, preset: str | None = None,
              emphasis: list[str] | tuple = (), hook: dict | None = None,
              caption_y: int | None = None, hook_y: int | None = None, font: str | None = None) -> str:
    """`words` use source-clip time; output times are relative to `clip_start`.

    style:   the studio.yaml `captions` section (size, words_per_line, uppercase, ...)
    preset:  one of PRESETS (falls back to style["preset"] / pop_yellow)
    hook:    {"text": ..., "seconds": 2.5, "color": "#RRGGBB"} title at the top, or None
    caption_y / hook_y: vertical centre of the caption line / hook, in pixels
    """
    p = PRESETS.get(preset or style.get("preset") or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    font = font or style.get("font") or DEFAULT_FONT
    if font == "Arial":            # old studio.yaml default: use the bundled bold font instead
        font = DEFAULT_FONT
    size = int(int(style.get("size", 104)) * p["size"])
    if caption_y is None:
        caption_y = int(height * float(style.get("position", 0.70)))
    upper = style.get("uppercase", True)
    emph = {_norm(e) for phrase in emphasis for e in phrase.split()}
    length = clip_end - clip_start

    words = [Word(w.start - clip_start, w.end - clip_start, w.text) for w in words
             if w.end > clip_start and w.start < clip_end]

    border_style = 3 if p["box"] else 1
    back = _ass_color("#000000", p.get("box_alpha", "80")) if p["box"] else _ass_color("#000000", "60")
    hook_color = (hook or {}).get("color") or "#FFFFFF"
    hook_box = (hook or {}).get("box_color") or "#E8202A"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{_ass_color(p["text"])},{_ass_color(p["text"])},{_ass_color(p["outline"])},{back},-1,0,0,0,100,100,1,0,{border_style},{p["outline_w"]},{p["shadow"]},5,60,60,0,1
Style: Hook,{font},{int(size * 0.82)},{_ass_color(hook_color)},{_ass_color(hook_color)},{_ass_color(hook_box)},{_ass_color(hook_box)},-1,0,0,0,100,100,1,0,3,22,0,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    cx = width // 2

    # hook title: pops in, holds, fades out
    if hook and hook.get("text"):
        secs = min(float(hook.get("seconds", 2.5)), max(length - 0.5, 0.5))
        hy = hook_y if hook_y is not None else int(height * 0.16)
        text = _clean(hook["text"].upper() if upper else hook["text"])
        anim = r"{\an5\pos(%d,%d)\fscx60\fscy60\t(0,140,\fscx108\fscy108)\t(140,240,\fscx100\fscy100)\fad(0,220)}" % (cx, hy)
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(secs)},Hook,,0,0,0,,{anim}{text}")

    max_chars = int(style.get("max_chars") or round(16 * 96 / max(size, 1)))   # ~16 caps fit at size 96
    groups = chunk(words, int(style.get("words_per_line", 3)), max_chars)
    active, emph_c, base = _ass_color(p["active"]), _ass_color(p["emph"]), _ass_color(p["text"])
    pop = int(p["pop"] * 100)
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
            parts = []
            for j, t in enumerate(texts):
                is_emph = _norm(group[j].text) in emph
                if j == i:   # the word being spoken: colour + pop
                    parts.append(r"{\c%s\fscx%d\fscy%d\t(0,90,\fscx%d\fscy%d)\t(90,180,\fscx%d\fscy%d)}%s{\c%s\fscx100\fscy100}"
                                 % (active + "&", 85, 85, pop, pop, 100 + (pop - 100) // 3, 100 + (pop - 100) // 3,
                                    t, base + "&"))
                elif is_emph:
                    parts.append(r"{\c%s}%s{\c%s}" % (emph_c + "&", t, base + "&"))
                else:
                    parts.append(t)
            pos = r"{\an5\pos(%d,%d)}" % (cx, caption_y)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{pos}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"
