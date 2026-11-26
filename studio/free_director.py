"""The free, built-in director: plans an edit with no AI and no internet.

It works out *what kind* of moment the clip is (funny, shock, hype, fail, clutch,
wholesome) from what chat spammed and what was said, finds the real payoff in the audio
(the loudest second near the chat spike), and builds the edit around that:
  - zoom + sound hit exactly on the payoff
  - start at a sentence boundary, end once the reaction has landed
  - a hook in the clip's language: a line actually said > what chat spammed > a template
  - caption style, sounds and post caption that fit the moment type
  - a quality score from chat strength, audio strength and speech
Choices rotate per campaign so consecutive clips don't look the same.
"""
from __future__ import annotations

import random
import re
import statistics

from studio.editor import trim
from studio.editor.transcribe import Word

TYPES = ("funny", "shock", "hype", "fail", "clutch", "wholesome")

CHAT_WORDS = {
    "funny": {"KEKW", "LUL", "LULW", "OMEGALUL", "LMAO", "LMFAO", "LOL", "ICANT", "KEK", "HAHA", "HAHAHA", "💀", "😂", "🤣",
              "XD", "PEPELAUGH", "JAJAJA"},
    "shock": {"???", "????", "?????", "WTF", "WHAT", "NO WAY", "HOLY", "OMG", "MONKAS", "MONKAW", "😱", "😳", "WAIT",
              "WHAT?", "HUH", "BRO WHAT", "NAH"},
    "hype": {"W", "WW", "POG", "POGGERS", "POGCHAMP", "POGU", "LETS GO", "LETSGO", "LET'S GO", "EZ", "CLEAN", "🔥",
             "INSANE", "GOATED", "GOAT", "HYPE"},
    "fail": {"L", "LL", "F", "RIP", "OOF", "UNLUCKY", "BRUH", "FAIL", "SADGE", "THROW", "THROWING"},
    "clutch": {"CLUTCH", "1HP", "1 HP", "HOW", "HOW?", "NO SHOT", "NOSHOT"},
    "wholesome": {"❤", "❤️", "<3", "AWW", "AWWW", "CUTE", "WHOLESOME", "🥺", "PEEPOHAPPY"},
}
SPEECH = {
    "funny": re.compile(r"\b(ha(ha)+|lmao|i can'?t|bro what|that'?s so funny|ich kann nicht mehr|lol)\b", re.I),
    "shock": re.compile(r"\b(what|no way|oh my god|wait|holy|are you serious|was|warum|alter|digga)\b", re.I),
    "hype": re.compile(r"\b(let'?s go|yes+|we did it|insane|easy|clean|ja+|geil|endlich)\b", re.I),
    "fail": re.compile(r"\b(oh no|no+ no+|i'?m dead|died|dead|i'?m done|verstorben|tot|nein|scheiße|fuck)\b", re.I),
    "clutch": re.compile(r"\b(one hp|1 hp|last second|clutch|so close|how did i)\b", re.I),
    "wholesome": re.compile(r"\b(thank you|love you|so sweet|wholesome|danke)\b", re.I),
}

TEMPLATES = {
    "en": {
        "funny": ["I CAN'T BREATHE", "CHAT WAS CRYING", "HE DIDN'T MEAN TO SAY THAT", "THIS GOT OUT OF HAND", "WORST TIMING EVER"],
        "shock": ["HE DIDN'T SEE THAT COMING", "NOBODY EXPECTED THIS", "WAIT... WHAT?", "THIS CAN'T BE REAL", "CHAT WENT SILENT"],
        "hype": ["HE ACTUALLY DID IT", "CHAT WENT CRAZY", "CLEANEST PLAY EVER", "THIS IS WHY WE WATCH"],
        "fail": ["THIS WENT WRONG FAST", "IT ALL FELL APART", "INSTANT REGRET", "HE SHOULDN'T HAVE DONE THAT"],
        "clutch": ["LAST SECOND CLUTCH", "HOW DID HE SURVIVE THIS?", "NO WAY HE PULLED THAT OFF"],
        "wholesome": ["THIS MADE HIS DAY", "CHAT WAS SO WHOLESOME", "CUTEST MOMENT ON STREAM"],
    },
    "de": {
        "funny": ["ICH KANN NICHT MEHR", "DAS WAR NICHT GEPLANT", "CHAT IST ESKALIERT", "SCHLECHTESTES TIMING EVER"],
        "shock": ["DAMIT HAT KEINER GERECHNET", "WARTE... WAS?", "DAS KANN NICHT ECHT SEIN", "CHAT WAR SPRACHLOS"],
        "hype": ["ER HAT ES WIRKLICH GESCHAFFT", "CHAT DREHT DURCH", "SAUBERSTER MOVE EVER"],
        "fail": ["DAS GING SCHIEF", "SOFORT BEREUT", "DAS WAR EIN FEHLER", "ALLES GING KAPUTT"],
        "clutch": ["IN LETZTER SEKUNDE", "WIE HAT ER DAS ÜBERLEBT?"],
        "wholesome": ["DAS HAT SEINEN TAG GERETTET", "CHAT WAR SO LIEB"],
    },
    "es": {
        "funny": ["NO PUEDO MÁS", "EL CHAT LLORABA DE RISA", "ESTO SE SALIÓ DE CONTROL"],
        "shock": ["NADIE ESPERABA ESTO", "ESPERA... ¿QUÉ?", "ESTO NO PUEDE SER REAL"],
        "hype": ["LO HIZO DE VERDAD", "EL CHAT ENLOQUECIÓ"],
        "fail": ["TODO SALIÓ MAL", "ARREPENTIMIENTO INSTANTÁNEO"],
        "clutch": ["EN EL ÚLTIMO SEGUNDO"],
        "wholesome": ["ESTO LE ALEGRÓ EL DÍA"],
    },
}
CHAT_HOOK = {"en": 'CHAT SPAMMED "{p}"', "de": 'CHAT SPAMMT "{p}"', "es": 'EL CHAT SPAMEÓ "{p}"'}

STYLE_FOR = {"funny": "pop_yellow", "shock": "punch_red", "hype": "outline_white", "fail": "karaoke_box",
             "clutch": "outline_white", "wholesome": "pop_yellow"}
SFX_FOR = {"funny": ["pop", "ding"], "shock": ["boom"], "hype": ["bass_drop"], "fail": ["boom", "bass_drop"],
           "clutch": ["bass_drop"], "wholesome": ["ding"]}
EMOJI = {"funny": "💀", "shock": "😳", "hype": "🔥", "fail": "😭", "clutch": "😮‍💨", "wholesome": "🥹"}
CAPTION = {
    "en": {"funny": "I can't stop laughing at this {e}", "shock": "Did NOT expect that {e}", "hype": "Chat went crazy {e}",
           "fail": "This went so wrong {e}", "clutch": "How did he pull this off {e}", "wholesome": "This made my day {e}"},
    "de": {"funny": "Ich kann nicht mehr {e}", "shock": "Damit hat keiner gerechnet {e}", "hype": "Chat ist durchgedreht {e}",
           "fail": "Das ging so schief {e}", "clutch": "Wie hat er das geschafft {e}", "wholesome": "So schön {e}"},
}
STRONG = re.compile(r"\b(no way|what|wait|oh my god|omg|holy|let'?s go|insane|crazy|why|how|stop|no+|yes+|"
                    r"wtf|nein|was|warum|alter|digga|dead|died|verstorben|parachute|fallschirm)\b", re.I)


# ----------------------------------------------------------------------------- analysis
def classify(top_chat: list, words: list[Word], moment: float) -> tuple[str, dict]:
    """Moment type from what chat spammed (weighted by count) and what was said near the moment."""
    scores = {t: 0.0 for t in TYPES}
    for item in top_chat or []:
        p, n = str(item[0]).upper().strip(), float(item[1])
        for t, vocab in CHAT_WORDS.items():
            if p in vocab or any(tok in vocab for tok in p.split()):
                scores[t] += n
    near = " ".join(w.text for w in words if moment - 6 <= w.start <= moment + 4)
    for t, rx in SPEECH.items():
        scores[t] += 3 * len(rx.findall(near))
    best = max(scores, key=scores.get)
    # no evidence either way: "shock" hooks ("nobody expected this") fit almost any spike
    return (best if scores[best] > 0 else "shock"), scores


def payoff(loudness: list, moment: float, duration: float) -> dict:
    """Loudest second near the chat moment (the scream / explosion / laugh) and when it calms down.

    loudness: [(second, LUFS)] for the whole clip. Returns {"at", "strength_db", "calm_at"}.
    """
    if not loudness:
        return {"at": moment, "strength_db": 0.0, "calm_at": min(duration, moment + 4)}
    base = statistics.median(l for _, l in loudness)
    window = [(t, l) for t, l in loudness if moment - 6 <= t <= moment + 4] or loudness
    # Loud stretches (shouting + explosion) are often flat for seconds: of everything within 2 dB of
    # the loudest second, take the one closest to when chat exploded.
    top = max(l for _, l in window)
    t_peak, l_peak = min(((t, l) for t, l in window if l >= top - 2), key=lambda x: abs(x[0] - moment))
    calm = next((t for t, l in loudness if t > t_peak and l < base + 3), t_peak + 4)
    return {"at": float(t_peak) + 0.3, "strength_db": round(l_peak - base, 1), "calm_at": float(min(calm, t_peak + 6, duration))}


def lang_of(language: str | None) -> str:
    return language if language in TEMPLATES else "en"


def hook_candidates(kind: str, words: list[Word], top_chat: list, at: float, language: str | None) -> list[str]:
    """Best first: a short line said near the payoff, what chat spammed, then templates for this type."""
    lang = lang_of(language)
    out = []
    text = " ".join(w.text for w in words if at - 5 <= w.start <= at + 3)
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        s = re.sub(r"[^\w\s'?!äöüÄÖÜßáéíóúñ¿¡]", "", sentence).strip()
        n = len(s.split())
        if 2 <= n <= 8 and (STRONG.search(s) or s.endswith(("?", "!"))):
            out.append(s.upper())
    for item in top_chat or []:
        p, n = str(item[0]), int(item[1])
        if n >= 8 and 1 <= len(p) <= 12 and not p.startswith("@"):
            out.append(CHAT_HOOK[lang if lang in CHAT_HOOK else "en"].format(p=p))
            break
    out += TEMPLATES[lang][kind]
    return out


def quality(signals: dict, strength_db: float, has_speech: bool, viewer_clips: int = 0) -> int:
    z = float((signals or {}).get("z_hype") or 3)
    q = 3 + min(3.0, z / 3) + min(2.0, max(strength_db, 0) / 6) + (1 if has_speech else 0) + (1 if viewer_clips else 0)
    return int(min(10, max(1, round(q))))


# ----------------------------------------------------------------------------- plan
def rule_plan(ctx: dict, seed: int | None = None) -> dict:
    rng = random.Random(seed if seed is not None else hash((ctx["channel"], ctx["moment"])))
    c, words, dur = ctx["campaign"], ctx["words"], ctx["duration"]
    sig = ctx.get("signals") or {}
    top_chat = sig.get("top_chat") or []
    kind, _ = classify(top_chat, words, ctx["moment"])
    pay = payoff(ctx.get("loudness") or [], ctx["moment"], dur)
    at = pay["at"]

    # keep: build-up before the payoff, then until the reaction has landed (+2.5 s)
    start, end = trim.choose_window(dur, at, c.get("min_seconds"), c.get("max_seconds"),
                                    float(c.get("target_seconds") or 32), words, before_share=0.65)
    hi = float(c.get("max_seconds") or dur)
    end = min(dur, max(end, min(pay["calm_at"] + 2.5, start + hi)))

    fc = ctx["facecam"]
    layout = c.get("layout") or {"webcam": "facecam_split", "center_face": "face_crop"}.get(fc.get("kind"), "blur_fit")

    recent_styles = ctx.get("recent_styles", [])[-2:]
    style = c.get("caption_style") or STYLE_FOR[kind]
    if not c.get("caption_style") and style in recent_styles:      # keep consecutive clips looking different
        from studio.editor.captions import PRESETS
        style = rng.choice([s for s in PRESETS if s not in recent_styles] or list(PRESETS))

    recent_hooks = set(ctx.get("recent_hooks", []))
    hooks = [h for h in hook_candidates(kind, words, top_chat, at, ctx.get("language")) if h not in recent_hooks]
    hook = hooks[0] if hooks else rng.choice(TEMPLATES[lang_of(ctx.get("language"))][kind])

    strength = 0.18 if pay["strength_db"] >= 8 else 0.14 if pay["strength_db"] >= 4 else 0.11
    zooms = [{"at": round(at, 2), "strength": strength}]
    sounds = SFX_FOR[kind]
    sfx = [{"at": round(at - 0.05, 2), "sound": rng.choice(sounds)}]
    if kind in ("hype", "clutch") and at - start > 3:
        sfx.insert(0, {"at": round(at - 1.6, 2), "sound": "riser"})
    elif kind in ("shock", "fail"):
        sfx.insert(0, {"at": round(start, 2), "sound": "whoosh"})
    if pay["strength_db"] >= 10:          # the audio already hits hard: don't pile sound on top
        sfx = [s for s in sfx if s["sound"] in ("whoosh", "riser")]

    emph = []
    for w in words:
        clean = re.sub(r"[^\w'äöüÄÖÜß]", "", w.text)
        if at - 3 <= w.start <= at + 2 and (STRONG.search(w.text) or len(clean) >= 7) and clean.lower() not in {e.lower() for e in emph}:
            emph.append(clean)
    lang = lang_of(ctx.get("language"))
    streamer = ctx["channel"].split(":")[-1]
    cap = CAPTION.get(lang, CAPTION["en"])[kind].format(e=EMOJI[kind])
    q = quality(sig, pay["strength_db"], bool(words), int((ctx.get("viewer_clips") or 0)))
    why = (f"Free director: a {kind} moment ({'chat: ' + ', '.join(str(p[0]) for p in top_chat[:2]) if top_chat else 'from the transcript'}); "
           f"zoom and sound on the loudest second at {at:.1f}s (+{pay['strength_db']:.0f} dB).")
    return {
        "keep_start": round(start, 2), "keep_end": round(end, 2), "layout": layout, "game_x": 0.5,
        "webcam_box": {"present": False, "x": 0, "y": 0, "w": 0, "h": 0},
        "hook_text": hook if c.get("hook", True) else "", "hook_seconds": 2.5, "caption_style": style,
        "emphasis_words": [e for e in emph if e][:3], "zooms": zooms, "sfx": sfx,
        "logo_corner": c.get("logo_corner") or ("top_right" if c.get("logo_path") else "none"),
        "post_title": hook.title()[:80], "post_caption": f"{cap} (via {streamer})" if streamer else cap,
        "hashtags": [], "why": why, "quality": q, "skip": q <= 2,
        "skip_reason": "weak chat reaction, quiet audio and no speech" if q <= 2 else "",
        "_source": "free", "_kind": kind,
    }
