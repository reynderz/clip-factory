"""Transcribe a clip to word-level timestamps using faster-whisper."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel


@dataclass
class Word:
    start: float   # seconds from clip start
    end: float
    text: str


_MODEL: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _MODEL
    if _MODEL is None:
        # "base" is fast and good enough for short clips. Upgrade to
        # "small" or "medium" for better accuracy on quiet audio.
        _MODEL = WhisperModel("base", device="auto", compute_type="auto")
    return _MODEL


def transcribe(audio_or_video: Path) -> list[Word]:
    model = _get_model()
    segments, _info = model.transcribe(
        str(audio_or_video),
        word_timestamps=True,
        vad_filter=True,           # skip silences
    )
    words: list[Word] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            text = w.word.strip()
            if not text:
                continue
            words.append(Word(start=w.start, end=w.end, text=text))
    return words
