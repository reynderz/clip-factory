"""Detect moments where the streamer is reading a chat message.

Approach: we already transcribe approved clips. For any window of the VOD,
we compare the streamer's transcribed speech to the chat messages posted
in a lookback window and score the best match.

In the detection phase we don't yet have transcripts for every second of
the VOD (too expensive). Instead we transcribe ONCE, coarsely, and reuse.
For a 4-hour VOD, `faster-whisper` with the base model runs in ~15-25 min
on an M3 Pro. Cached to disk, so only pays the cost once per VOD.

Scoring:
    For every second S of the VOD:
      speech_window = transcript spanning [S-6s, S+2s]
      chat_window   = messages from [S-30s, S+5s]  (readers can lag)
      score         = max( token_set_ratio(speech_window, msg)
                           for msg in chat_window )
    We keep seconds where score >= threshold.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz

from .fetch import ChatMessage


@dataclass
class ChatReadEvent:
    peak_sec: float
    score: float                 # 0-100 fuzzy match
    message_idx: int             # index into the chat list for best match
    speech_excerpt: str


def _text_in_window(words, t0: float, t1: float) -> str:
    return " ".join(w["text"] for w in words
                    if t0 <= w["start"] <= t1).strip()


def _is_junk_message(text: str) -> bool:
    """Filter bot noise, pure emote spam, URLs."""
    t = text.strip()
    if not t or len(t) < 6:
        return True
    if t.startswith(("!", "http", "www.")):
        return True
    # Mostly-emotes (no spaces, all caps, very short tokens)
    tokens = t.split()
    if tokens and all(tok.isupper() and len(tok) <= 10 for tok in tokens):
        if len(tokens) <= 3:
            return True
    return False


def detect_chat_reads(vod_transcript_path: Path,
                      msgs: list[ChatMessage],
                      duration_sec: float,
                      cfg: dict) -> list[ChatReadEvent]:
    """Scan the VOD transcript for chat-reading moments."""
    if not vod_transcript_path.exists():
        return []
    words = json.loads(vod_transcript_path.read_text())
    if not words:
        return []

    c = cfg.get("chat_reading", {})
    threshold = c.get("similarity_threshold", 55)
    lookback = c.get("chat_lookback_sec", 30)
    lookahead = c.get("chat_lookahead_sec", 5)
    speech_before = c.get("speech_window_before_sec", 6)
    speech_after = c.get("speech_window_after_sec", 2)
    stride = c.get("scan_stride_sec", 2)    # don't check every second

    # Pre-filter chat to the relevant universe
    clean_msgs = [(i, m) for i, m in enumerate(msgs)
                  if not _is_junk_message(m.text)]

    events: list[ChatReadEvent] = []
    last_emitted = -1e9
    for s in range(0, int(duration_sec), stride):
        speech = _text_in_window(words, s - speech_before, s + speech_after)
        if len(speech) < 10:
            continue

        # candidate messages in the lookback window
        window_msgs = [(i, m) for i, m in clean_msgs
                       if s - lookback <= m.offset_sec <= s + lookahead]
        if not window_msgs:
            continue

        best_score = 0.0
        best_idx = -1
        for i, m in window_msgs:
            score = fuzz.token_set_ratio(speech.lower(), m.text.lower())
            if score > best_score:
                best_score = score
                best_idx = i

        if best_score >= threshold and s - last_emitted >= 15:
            events.append(ChatReadEvent(
                peak_sec=float(s),
                score=float(best_score),
                message_idx=best_idx,
                speech_excerpt=speech[:140],
            ))
            last_emitted = s

    return events


def save_events(events: list[ChatReadEvent], path: Path) -> None:
    path.write_text(json.dumps([asdict(e) for e in events], indent=2))


def load_events(path: Path) -> list[ChatReadEvent]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [ChatReadEvent(**d) for d in data]
