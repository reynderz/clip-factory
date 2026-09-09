"""Automatic jump cuts: trim dead air (silence between spoken words) out of
a clip so pacing stays tight — the same technique MrBeast/Hormozi-style
editors use by hand.

Works on the clip's word-level transcript (GUI-saved words, vod_transcript,
or a fresh whisper pass — whatever the caller already has). Any gap between
two consecutive words that's >= min_gap_sec becomes a cut. Long gaps are
capped at max_cut_sec (keeping pad_sec of silence on each side) so a natural
pause doesn't get sliced down to nothing.

Output feeds straight into the same `cuts` list used for manual jump cuts
(stages/render.py's _build_cuts_filter, stages/subtitle.py's
apply_cuts_to_words) — auto and manual cuts are just concatenated by the
caller since both consumers already merge overlapping/adjacent ranges.
"""
from __future__ import annotations


def _w_start(w) -> float:
    return float(w.start if hasattr(w, "start") else w["start"])


def _w_end(w) -> float:
    return float(w.end if hasattr(w, "end") else w["end"])


def detect_silence_cuts(words: list, duration: float, cfg: dict) -> list[dict]:
    """Return [{start, end}, ...] clip-relative ranges of dead air to remove.

    words can be transcribe.Word objects or raw dicts (start/end/text) —
    same duck-typed convention as stages/censor.py.
    """
    jc = cfg.get("jump_cuts", {})
    if not jc.get("enabled", False) or not words:
        return []

    min_gap = float(jc.get("min_gap_sec", 0.6))
    max_cut = float(jc.get("max_cut_sec", 3.0))
    pad = float(jc.get("pad_sec", 0.12))

    ordered = sorted(words, key=_w_start)
    cuts: list[dict] = []
    prev_end = 0.0

    def _add_gap(gap_start: float, gap_end: float) -> None:
        s, e = gap_start + pad, gap_end - pad
        if e - s < 0.05:
            return
        if e - s > max_cut:
            mid = (s + e) / 2
            s, e = mid - max_cut / 2, mid + max_cut / 2
        cuts.append({"start": round(s, 3), "end": round(e, 3)})

    for w in ordered:
        ws = _w_start(w)
        if ws - prev_end >= min_gap:
            _add_gap(prev_end, ws)
        prev_end = max(prev_end, _w_end(w))

    if duration - prev_end >= min_gap:
        _add_gap(prev_end, duration)

    return cuts
