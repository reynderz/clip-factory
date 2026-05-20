"""Given a moment in the VOD, return the top-K candidate chat messages
the streamer might be reading, ranked by a combined score.

Used during the review step so you can confirm or swap the message before
rendering.

Ranking signal =
    0.7 * token_set_ratio(speech, msg.text)
  + 0.2 * recency_bonus (closer to the moment = higher)
  + 0.1 * length_bonus  (longer messages > emote spam)
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .detect_chat_reading import _text_in_window, _is_junk_message
from .fetch import ChatMessage


@dataclass
class MessageCandidate:
    msg_idx: int
    user: str
    text: str
    offset_sec: float
    score: float


def rank_messages(peak_sec: float,
                  vod_transcript_path: Path,
                  msgs: list[ChatMessage],
                  cfg: dict,
                  top_k: int = 5) -> list[MessageCandidate]:
    if not vod_transcript_path.exists():
        return []
    words = json.loads(vod_transcript_path.read_text())
    c = cfg.get("chat_reading", {})
    lookback = c.get("chat_lookback_sec", 30)
    lookahead = c.get("chat_lookahead_sec", 5)
    speech_before = c.get("speech_window_before_sec", 6)
    speech_after = c.get("speech_window_after_sec", 2)

    speech = _text_in_window(
        words, peak_sec - speech_before, peak_sec + speech_after).lower()
    if not speech:
        return []

    window = [(i, m) for i, m in enumerate(msgs)
              if peak_sec - lookback <= m.offset_sec <= peak_sec + lookahead
              and not _is_junk_message(m.text)]

    scored: list[MessageCandidate] = []
    for i, m in window:
        sim = fuzz.token_set_ratio(speech, m.text.lower())
        # recency: 1.0 if posted right before peak, fades with distance
        dt = abs(peak_sec - m.offset_sec)
        recency = max(0.0, 1.0 - dt / lookback) * 100
        length_bonus = min(len(m.text) / 80.0, 1.0) * 100
        score = 0.7 * sim + 0.2 * recency + 0.1 * length_bonus
        scored.append(MessageCandidate(
            msg_idx=i, user=m.user, text=m.text,
            offset_sec=m.offset_sec, score=score,
        ))

    scored.sort(key=lambda c: -c.score)
    return scored[:top_k]
