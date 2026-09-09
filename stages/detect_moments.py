"""Find clip-worthy moments in a VOD.

Strategy: independent signals, merged.
  1. Chat velocity: messages/sec in a sliding window. High = hype.
  2. Audio RMS peaks: loud moments (laughter, screams, game sounds).
  3. Mic-band RMS: RMS restricted to the voice frequency band, isolating
     the streamer's mic from bass-heavy game audio. A stricter, higher
     percentile within that same band flags screaming specifically,
     since it's a stronger hype signal than an ordinary raised voice.

There's no separate mic-only audio track (Twitch VODs ship one mixed
stereo track), so "mic peak" is a band-pass proxy rather than a true
isolated channel.

A candidate is scored as a weighted sum. We merge overlapping/nearby
candidates, capping merged length at `max_candidate_duration_sec` so a
long sustained-hype stretch doesn't chain into one giant candidate.
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


def mic_band_rms(vod_path: Path, duration_sec: float, window: float,
                  low_hz: float, high_hz: float) -> np.ndarray:
    """Per-second RMS energy within [low_hz, high_hz], via ffmpeg.

    Band-passing to the voice range before computing RMS approximates an
    isolated mic signal: a mixed track's bass/sub content (game music, low
    rumble) is mostly filtered out, so peaks here track the streamer's
    voice rather than the game.
    """
    cmd = [
        "ffmpeg", "-i", str(vod_path),
        "-af", f"highpass=f={low_hz},lowpass=f={high_hz}",
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
    kernel = np.ones(int(window)) / window
    rms = np.convolve(rms, kernel, mode="same")
    return rms


# ─────────────────────────── merge ─────────────────────────────
def _merge(cands: list[Candidate], min_gap: float,
           max_duration: float | None = None) -> list[Candidate]:
    if not cands:
        return []
    cands.sort(key=lambda c: c.start_sec)
    out = [cands[0]]
    for c in cands[1:]:
        last = out[-1]
        merged_end = max(last.end_sec, c.end_sec)
        fits = max_duration is None or merged_end - last.start_sec <= max_duration
        if c.start_sec - last.end_sec < min_gap and fits:
            last.end_sec = merged_end
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

    # --- mic-band peaks (voice) + screaming (stricter percentile, same band)
    mic = mic_band_rms(vod_path, duration_sec, d["mic_window_sec"],
                        d["mic_band_low_hz"], d["mic_band_high_hz"])
    mic_thresh = np.percentile(mic, d["mic_peak_percentile"])
    scream_thresh = np.percentile(mic, d["scream_percentile"])

    n = len(rate)
    score = np.zeros(n)
    reasons_per_sec: list[list[str]] = [[] for _ in range(n)]
    for sec in range(n):
        if rate[sec] >= chat_thresh:
            reasons_per_sec[sec].append(f"chat={rate[sec]:.1f}/s")
            score[sec] += rate[sec] / chat_thresh      # normalized
        if sec < len(rms) and rms[sec] >= audio_thresh:
            reasons_per_sec[sec].append(f"audio={rms[sec]:.0f}")
            score[sec] += 1.0
        if sec < len(mic) and mic[sec] >= scream_thresh:
            # Screaming implies mic peak too; count it once, at the higher weight.
            reasons_per_sec[sec].append(f"scream={mic[sec]:.0f}")
            score[sec] += d["scream_weight"]
        elif sec < len(mic) and mic[sec] >= mic_thresh:
            reasons_per_sec[sec].append(f"mic={mic[sec]:.0f}")
            score[sec] += d["mic_weight"]

    # Greedy peak-picking with non-max suppression. A plain interval-merge
    # collapses any long stretch of consecutive above-threshold seconds
    # (e.g. a sustained-hype segment) into one giant multi-minute blob,
    # which counts as a single candidate and starves the rest of the VOD
    # of slots. Picking local maxima and suppressing a min_gap_sec zone
    # around each one instead yields many short, well-centered candidates
    # from the same stretch.
    order = np.argsort(-score)
    taken = np.zeros(n, dtype=bool)
    cands: list[Candidate] = []
    for sec in order:
        if score[sec] <= 0:
            break  # sorted descending — nothing left crosses a threshold
        if taken[sec]:
            continue
        start = float(max(0.0, sec - d["clip_lead_sec"]))
        end = float(min(duration_sec, sec + d["clip_trail_sec"]))
        cands.append(Candidate(
            start_sec=start,
            end_sec=end,
            peak_sec=float(sec),
            score=float(score[sec]),
            reasons=reasons_per_sec[sec],
        ))
        lo = max(0, int(start - d["min_gap_sec"]))
        hi = min(n, int(end + d["min_gap_sec"]) + 1)
        taken[lo:hi] = True

    cands.sort(key=lambda c: -c.score)
    return cands
