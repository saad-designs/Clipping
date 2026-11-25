"""Word-level transcript with faster-whisper. Optional: without it, clips get no captions."""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("whisper")
_models: dict = {}


@dataclass
class Word:
    start: float
    end: float
    text: str


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _model(name: str):
    if name not in _models:
        from faster_whisper import WhisperModel
        # int8 on CPU is fast enough for 1-minute clips; CUDA is used automatically when present.
        try:
            _models[name] = WhisperModel(name, device="cuda", compute_type="float16")
        except Exception:
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _models[name]


def transcribe(path, model: str = "small", language: str | None = None) -> list[Word]:
    if not available():
        log.warning("faster-whisper not installed: no captions (pip install faster-whisper)")
        return []
    try:
        segments, _info = _model(model).transcribe(str(path), language=language, word_timestamps=True,
                                                   vad_filter=True, beam_size=5)
        return [Word(w.start, w.end, w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]
    except Exception as e:   # a missing model download, a corrupt file: still make the clip
        log.warning("transcription failed (%s): clip will have no captions", e)
        return []
