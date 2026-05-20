"""Detect profanity in word-level transcripts for audio censoring."""
from __future__ import annotations

_DEFAULT_WORDS = {
    "fuck", "fucked", "fucking", "fucker", "fucks", "fuckin",
    "shit", "shitting", "shitty", "shits",
    "ass", "asses", "asshole", "assholes",
    "bitch", "bitches", "bitching",
    "cunt", "cunts",
    "bastard", "bastards",
    "piss", "pissed", "pissing",
    "dick", "cock", "cocks",
    "pussy", "pussies",
    "whore", "slut",
    "nigga", "nigger",
    "faggot", "fag",
}


def find_curse_words(words: list, cfg: dict) -> list[dict]:
    """Return {start, end} dicts (seconds from clip start) for each profane word.

    words can be Word namedtuples or dicts with text/start/end fields.
    """
    censor_cfg = cfg.get("censor", {})
    if not censor_cfg.get("enabled", True):
        return []

    blocklist = set(_DEFAULT_WORDS)
    for w in censor_cfg.get("extra_words", []):
        blocklist.add(w.lower())
    for w in censor_cfg.get("exclude_words", []):
        blocklist.discard(w.lower())

    pad = float(censor_cfg.get("pad_sec", 0.05))
    hits = []
    for w in words:
        text          = (w.text          if hasattr(w, "text")          else w["text"])
        start         = (w.start         if hasattr(w, "start")         else w["start"])
        end           = (w.end           if hasattr(w, "end")           else w["end"])
        # original_text is the pre-replacement text stored by the GUI when the
        # user censors a word and changes the subtitle display text.
        original_text = w.get("original_text", text) if isinstance(w, dict) else text
        # Explicit censored flag from GUI editor: True = always bleep,
        # False = never bleep (user override), absent = auto-detect.
        explicit = w.get("censored") if isinstance(w, dict) else None

        if explicit is False:
            continue  # user explicitly uncensored this word
        if explicit is True:
            hits.append({"start": max(0.0, float(start) - pad), "end": float(end) + pad})
            continue
        # Auto-detect: check original_text so replacement text (****) doesn't break it
        clean = original_text.lower().strip(".,!?;:'\"()-")
        if clean in blocklist:
            hits.append({"start": max(0.0, float(start) - pad), "end": float(end) + pad})
    return hits
