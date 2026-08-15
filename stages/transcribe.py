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
        _MODEL = WhisperModel("small", device="auto", compute_type="auto")
    return _MODEL


def transcribe(audio_or_video: Path) -> list[Word]:
    model = _get_model()
    segments, _info = model.transcribe(
        str(audio_or_video),
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
    )
    words: list[Word] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            text = w.word.strip()
            if not text:
                continue
            # Drop zero-duration words (timing artifacts from silent sections)
            if w.end <= w.start:
                continue
            words.append(Word(start=w.start, end=w.end, text=text))
    return words
