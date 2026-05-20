"""One-shot coarse transcription of the entire VOD, cached to disk.

Used by the chat-reading detector. We don't need perfect accuracy —
token-set-ratio matching is robust to moderate transcription errors.
Base model on CPU is the right speed/quality tradeoff here.
"""
from __future__ import annotations
import json
from pathlib import Path

from .transcribe import _get_model


def transcribe_full_vod(vod_path: Path, out_path: Path) -> Path:
    """Writes a JSON list of {start, end, text} word objects. Idempotent."""
    if out_path.exists():
        return out_path

    model = _get_model()
    segments, _ = model.transcribe(
        str(vod_path),
        word_timestamps=True,
        vad_filter=True,
        # Speed up coarse pass; we're not going for perfection.
        beam_size=1,
    )

    words: list[dict] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            text = (w.word or "").strip()
            if text:
                words.append({
                    "start": float(w.start),
                    "end": float(w.end),
                    "text": text,
                })

    out_path.write_text(json.dumps(words))
    return out_path
