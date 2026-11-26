"""Words and emotes that chat spams when something big happens.

Matching is done per message (a message counts once no matter how many tokens it holds),
so one person pasting "KEKW KEKW KEKW" doesn't count more than one "KEKW".
"""
import re

HYPE_TOKENS = {
    # laughing
    "LOL", "LMAO", "LMFAO", "KEKW", "KEK", "OMEGALUL", "LUL", "LULW", "ICANT", "💀", "😂", "🤣",
    # shock / hype
    "POG", "POGGERS", "POGCHAMP", "POGU", "WOW", "OMG", "WTF", "HOLY", "INSANE", "NO WAY",
    "MONKAS", "MONKAW", "🔥", "😱",
    # verdicts
    "W", "L", "WW", "LL", "RATIO", "DUB",
    # explicit asks
    "CLIP", "CLIPIT", "CLIP IT", "CLIPPED", "CLIPPING",
}

_QUESTION_SPAM = re.compile(r"\?{3,}")
_REPEATED_LETTER = re.compile(r"([A-Z])\1{3,}")  # NOOOOO, AAAAA


def is_hype(message: str, extra: set[str] | frozenset[str] = frozenset()) -> bool:
    upper = message.upper()
    if _QUESTION_SPAM.search(upper) or _REPEATED_LETTER.search(upper):
        return True
    words = re.findall(r"[\w']+|[^\w\s]", upper)
    tokens = HYPE_TOKENS | extra
    for w in words:
        if w in tokens:
            return True
    # multi-word tokens
    return any(" " in t and t in upper for t in tokens)


# Chat bots and commands aren't reactions: leave them out of every count.
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "botrixoficial",
        "kickbot", "sery_bot", "soundalerts", "pokemoncommunitygame", "blerp", "own3d", "frostytoolsdotcom"}


def is_noise(user: str, text: str) -> bool:
    u = user.lower()
    return text.startswith("!") or u in BOTS or u.endswith("bot")
