"""Find clip-worthy moments in a VOD.

Strategy: two independent signals, merged.
  1. Chat velocity: messages/sec in a sliding window. High = hype.
  2. Audio RMS peaks: loud moments (laughter, screams, game sounds).

A candidate is scored as a weighted sum. We merge overlapping candidates
and cap at `max_candidates_per_vod`.
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fetch import ChatMessage


@dataclass
class Candidate:
    start_sec: float
    end_sec: float
    peak_sec: float       # timestamp of the signal peak (for preview)
    score: float
    reasons: list[str]    # e.g. ["chat=8.3/s", "audio=97pct"]


# ───────────────────────── chat signal ─────────────────────────
def chat_velocity(msgs: list[ChatMessage], duration_sec: float,
                  window: float) -> np.ndarray:
    """Returns per-second messages/sec computed over a sliding window."""
    n_sec = int(duration_sec) + 1
    counts = np.zeros(n_sec)
    for m in msgs:
        idx = int(m.offset_sec)
        if 0 <= idx < n_sec:
            counts[idx] += 1
    # sliding window sum → rate
    kernel = np.ones(int(window))
    rate = np.convolve(counts, kernel, mode="same") / window
    return rate


# ───────────────────────── audio signal ────────────────────────
def audio_rms(vod_path: Path, duration_sec: float,
              window: float) -> np.ndarray:
    """Per-second RMS energy via ffmpeg."""
    # Extract mono 16kHz PCM, compute RMS in python
    cmd = [
        "ffmpeg", "-i", str(vod_path),
        "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    sr = 16000
    samples_per_sec = sr
    n_sec = int(duration_sec) + 1
    rms = np.zeros(n_sec)
    for s in range(n_sec):
        chunk = pcm[s * samples_per_sec:(s + 1) * samples_per_sec]
        if len(chunk):
            rms[s] = float(np.sqrt(np.mean(chunk ** 2)))
    # smooth
    kernel = np.ones(int(window)) / window
    rms = np.convolve(rms, kernel, mode="same")
    return rms


# ─────────────────────────── merge ─────────────────────────────
def _merge(cands: list[Candidate], min_gap: float) -> list[Candidate]:
    if not cands:
        return []
    cands.sort(key=lambda c: c.start_sec)
    out = [cands[0]]
    for c in cands[1:]:
        last = out[-1]
        if c.start_sec - last.end_sec < min_gap:
            last.end_sec = max(last.end_sec, c.end_sec)
            last.score = max(last.score, c.score)
            last.reasons = list(set(last.reasons + c.reasons))
        else:
            out.append(c)
    return out


# ─────────────────────────── main ──────────────────────────────
def detect(vod_path: Path, msgs: list[ChatMessage], duration_sec: float,
           cfg: dict) -> list[Candidate]:
    d = cfg["detection"]

    # --- chat peaks
    rate = chat_velocity(msgs, duration_sec, d["chat_window_sec"])
    chat_thresh = d["chat_min_msgs_per_sec"]

    # --- audio peaks
    rms = audio_rms(vod_path, duration_sec, d["audio_window_sec"])
    audio_thresh = np.percentile(rms, d["audio_peak_percentile"])

    cands: list[Candidate] = []
    for sec in range(len(rate)):
        reasons, score = [], 0.0
        if rate[sec] >= chat_thresh:
            reasons.append(f"chat={rate[sec]:.1f}/s")
            score += rate[sec] / chat_thresh      # normalized
        if sec < len(rms) and rms[sec] >= audio_thresh:
            reasons.append(f"audio={rms[sec]:.0f}")
            score += 1.0
        if reasons:
            cands.append(Candidate(
                start_sec=max(0.0, sec - d["clip_lead_sec"]),
                end_sec=min(duration_sec, sec + d["clip_trail_sec"]),
                peak_sec=float(sec),
                score=score,
                reasons=reasons,
            ))

    cands = _merge(cands, d["min_gap_sec"])
    cands.sort(key=lambda c: -c.score)
    return cands[:d["max_candidates_per_vod"]]
