"""Choose which part of a clip to keep."""
from __future__ import annotations

from studio.editor.transcribe import Word


def choose_window(duration: float, moment: float | None, min_s: float | None, max_s: float | None,
                  target: float, words: list[Word] | None = None, before_share: float = 0.6) -> tuple[float, float]:
    """Returns (start, end) in seconds.

    Keeps `target` seconds (bounded by the campaign's min/max and the clip itself), with ~60%
    before the moment for build-up and ~40% after for the reaction. Edges move so they don't
    cut a word in half.
    """
    lo = float(min_s or 0)
    hi = float(max_s or duration)
    length = min(max(target, lo), hi, duration)
    if moment is None:
        moment = duration / 2
    start = moment - length * before_share
    start = max(0.0, min(start, duration - length))
    end = start + length
    if words:
        start, end = _snap(start, end, words, duration, lo, hi)
    return round(start, 2), round(end, 2)


def _snap(start: float, end: float, words: list[Word], duration: float, lo: float, hi: float) -> tuple[float, float]:
    for w in words:   # start inside a word → begin at that word
        if w.start < start < w.end:
            start = max(0.0, w.start - 0.05)
            break
    for w in words:   # end inside a word → let the word finish (small extension only)
        if w.start < end < w.end and w.end - end < 2.0:
            end = min(duration, w.end + 0.15)
            break
    if end - start > hi:          # never break the campaign's max length
        end = start + hi
    if end - start < lo:
        end = min(duration, start + lo)
    return start, end
