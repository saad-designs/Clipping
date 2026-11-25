"""Loads studio.yaml (laptop side). Missing keys use the defaults below."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS = {
    "server_url": "http://127.0.0.1:8787",
    "token": "",
    "output_dir": "Campaigns",
    "poll_minutes": 3,
    "editing": {"target_seconds": 35, "layout": "blur", "blur_zoom": 1.25, "fps": 30, "encoder": "auto"},
    "captions": {"enabled": True, "whisper_model": "small", "language": None, "font": "Arial", "size": 96,
                 "words_per_line": 3, "uppercase": True, "highlight_color": "#FFD21F", "position": 0.70},
    "facecam": {},
    "vod": {"top": 8, "min_gap_seconds": 90, "whisper_model": "base"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str | Path = "studio.yaml") -> dict:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg = _merge(DEFAULTS, raw or {})
    out = Path(cfg["output_dir"]).expanduser()
    # a relative output_dir means "next to studio.yaml", wherever you run the command from
    cfg["output_dir"] = out if out.is_absolute() else (p.resolve().parent / out)
    return cfg
