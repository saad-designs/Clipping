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


_cpu_only = False   # set once the GPU fails: an NVIDIA card without the CUDA libraries (cublas/cudnn)


def _model(name: str):
    if name not in _models:
        from faster_whisper import WhisperModel
        # int8 on CPU is fast enough for 1-minute clips; CUDA is used automatically when present.
        try:
            if _cpu_only:
                raise RuntimeError("GPU disabled")
            _models[name] = WhisperModel(name, device="cuda", compute_type="float16")
        except Exception:
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _models[name]


def _is_gpu_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("cuda", "cublas", "cudnn", "gpu"))


def _run(model: str, audio, **kw):
    """Transcribe; if the GPU turns out unusable mid-run (CUDA libraries missing), redo it on the CPU."""
    global _cpu_only
    try:
        segments, info = _model(model).transcribe(audio, **kw)
        return list(segments), info          # segments are lazy: the GPU work (and its errors) happen here
    except Exception as e:
        if _cpu_only or not _is_gpu_error(e):
            raise
        log.warning("whisper can't use the GPU (%s): using the CPU instead", str(e)[:120])
        _cpu_only = True
        _models.clear()
        segments, info = _model(model).transcribe(audio, **kw)
        return list(segments), info


def self_test(model: str = "small") -> str:
    """Actually run whisper on 2 s of tone. Returns 'GPU' or 'CPU'; raises if it can't run at all."""
    import numpy as np
    t = np.arange(32000, dtype=np.float32) / 16000
    _run(model, (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), vad_filter=False, beam_size=1)
    return "CPU" if _cpu_only or getattr(_models[model].model, "device", "cpu") == "cpu" else "GPU"


def load_audio(path) -> "np.ndarray":
    """16 kHz mono float audio via ffmpeg. faster-whisper's own decoder (PyAV) breaks when
    pip installs a newer PyAV than it expects, so we don't rely on it."""
    import subprocess

    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def transcribe_full(path, model: str = "small", language: str | None = None) -> tuple[list[Word], str | None]:
    """Words plus the spoken language whisper detected (e.g. "en", "de")."""
    if not available():
        log.warning("faster-whisper not installed: no captions (pip install faster-whisper)")
        return [], language
    try:
        audio = load_audio(path)
        if audio.size == 0:
            return [], language
        segments, info = _run(model, audio, language=language, word_timestamps=True, vad_filter=True, beam_size=5)
        words = [Word(float(w.start), float(w.end), w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]
        return words, getattr(info, "language", None) or language
    except Exception as e:   # a missing model download, a corrupt file: still make the clip
        log.warning("transcription failed (%s): clip will have no captions", e)
        return [], language


def transcribe(path, model: str = "small", language: str | None = None) -> list[Word]:
    return transcribe_full(path, model, language)[0]
