"""Transcribe a clip to word-level timestamps using faster-whisper."""
from __future__ import annotations
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    # ctranslate2 dlopen's cuBLAS/cuDNN lazily, on the first real GPU call
    # (not at WhisperModel construction), and it does NOT search
    # site-packages\nvidia\*\bin on its own even though pip put the DLLs
    # there. ctranslate2's native loader calls LoadLibraryA without the
    # search flags that respect os.add_dll_directory(), so the only thing
    # that reliably works is putting these directories on PATH itself
    # (the classic DLL search order always consults PATH).
    import sysconfig
    _site_packages = Path(sysconfig.get_paths()["purelib"])
    _extra = [
        str(_site_packages / "nvidia" / _pkg / "bin")
        for _pkg in ("cublas", "cudnn", "cuda_nvrtc")
        if (_site_packages / "nvidia" / _pkg / "bin").is_dir()
    ]
    if _extra:
        os.environ["PATH"] = os.pathsep.join(_extra) + os.pathsep + os.environ.get("PATH", "")
        for _dir in _extra:
            os.add_dll_directory(_dir)

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
