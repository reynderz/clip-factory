"""Generate styled .ass subtitles with per-word highlight.

Output is Advanced SubStation Alpha (.ass) because it supports inline
color overrides, which is how we do the per-word yellow 'active' effect.

One line shows up to N words. For each word slot, we emit a sub-event
that lasts exactly while that word is spoken; during that event, the
active word is painted in `highlight_color`, inactive words in
`primary_color`.
"""
from __future__ import annotations
from pathlib import Path

from .transcribe import Word


def _pct_to_ass_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _group_lines(words: list[Word], max_per_line: int) -> list[list[Word]]:
    return [words[i:i + max_per_line]
            for i in range(0, len(words), max_per_line)]


def _ass_color_override(color: str) -> str:
    # ASS inline override: {\1c&HBBGGRR&}
    # config uses &H00BBGGRR format; strip the alpha byte for inline.
    hex_part = color.replace("&H", "").replace("&", "")
    if len(hex_part) == 8:
        hex_part = hex_part[2:]
    return "{\\1c&H" + hex_part + "&}"


# MrBeast-style word_pop defaults, substituted when style=word_pop and the
# config's pop_scale/pop_duration_ms were left at their karaoke "off" values.
# Bigger overshoot + a fast settle reads as punchy/fast-paced rather than
# floaty; real MrBeast-style generators use a quick spring pop, not a slow ease.
WORD_POP_SCALE = 200
WORD_POP_DURATION_MS = 90


def _build_word_pop_events(words: list[Word], s: dict,
                            text_ovr: str, pop_tag: str,
                            delay_sec: float) -> list[str]:
    """One word on screen at a time. Optionally pops a filler during long gaps.

    Each word is capped at max_word_display_sec — without this, a slowly
    enunciated word just sits still on screen, which reads as sluggish
    rather than "fast paced" regardless of how snappy the pop-in animation is.
    """
    all_caps = s["all_caps"]
    gap_pop_sec = float(s.get("gap_pop_sec", 0.0))
    gap_pop_text = s.get("gap_pop_text", "...")
    gap_pop_max_sec = float(s.get("gap_pop_max_sec", 1.2))
    max_display = float(s.get("max_word_display_sec", 0.45))

    events: list[str] = []
    for i, w in enumerate(words):
        text = w.text.upper() if all_caps else w.text
        start = w.start + delay_sec
        word_end = w.end if max_display <= 0 else min(w.end, w.start + max_display)
        end = word_end + delay_sec
        if end > start:
            events.append(
                f"Dialogue: 0,{_pct_to_ass_time(start)},"
                f"{_pct_to_ass_time(end)},Default,,0,0,0,,"
                f"{pop_tag}{text_ovr}{text}"
            )

        if i + 1 < len(words) and gap_pop_sec > 0:
            gap_start = w.end
            gap = words[i + 1].start - gap_start
            if gap >= gap_pop_sec:
                g_start = gap_start + delay_sec
                g_end = gap_start + min(gap, gap_pop_max_sec) + delay_sec
                if g_end > g_start:
                    events.append(
                        f"Dialogue: 0,{_pct_to_ass_time(g_start)},"
                        f"{_pct_to_ass_time(g_end)},Default,,0,0,0,,"
                        f"{pop_tag}{text_ovr}{gap_pop_text}"
                    )
    return events


def get_style(s: dict) -> str:
    return s.get("style") or ("word_pop" if s.get("word_by_word", False) else "karaoke")


def effective_font_size(s: dict) -> int:
    """Resting font size in px, accounting for word_pop's size bump."""
    font_size = float(s["font_size"])
    if get_style(s) == "word_pop":
        font_size *= float(s.get("word_pop_font_scale", 1.2))
    return int(round(font_size))


def build_ass(words: list[Word], cfg: dict, video_size: tuple[int, int],
              delay_sec: float = 0.0) -> str:
    s = cfg["subs"]
    vw, vh = video_size

    if get_style(s) == "none":
        # Header-only ASS (no Dialogue events) — render.py always applies the
        # `ass` filter, so this is the simplest way to render zero subtitles
        # without touching the filter graph.
        return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    primary = s["primary_color"]
    highlight = s["highlight_color"]
    outline = s["outline_color"]

    margin_v = int(vh * (1.0 - s["position_y_frac"]))

    word_by_word = get_style(s) == "word_pop"
    font_size = effective_font_size(s)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{s["font"]},{font_size},{primary},{primary},{outline},&H00000000,-1,0,0,0,100,100,0,0,1,{s["outline"]},{s["shadow"]},2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    primary_ovr = _ass_color_override(primary)
    highlight_ovr = _ass_color_override(highlight)

    pop_scale = int(s.get("pop_scale", 100))
    pop_ms = int(s.get("pop_duration_ms", 0))
    if word_by_word and pop_scale == 100 and pop_ms == 0:
        # Karaoke's "off" defaults; word_pop gets a real bounce out of the box.
        pop_scale = WORD_POP_SCALE
        pop_ms = WORD_POP_DURATION_MS
    if pop_scale != 100 and pop_ms > 0:
        if word_by_word:
            # \fscx/\fscy scaling grows around the line's \an anchor point.
            # The style default is bottom-center (\an2), so the pop grows
            # upward from the baseline. Re-anchor to middle-center (\an5)
            # with an explicit \pos so it pops from the text's own center
            # instead of its bottom edge.
            pos_x = vw // 2
            pos_y = int(vh - margin_v - font_size * 0.4)
            anchor_tag = f"\\an5\\pos({pos_x},{pos_y})"
        else:
            anchor_tag = ""
        pop_tag = f"{{{anchor_tag}\\fscx{pop_scale}\\fscy{pop_scale}\\t(0,{pop_ms},\\fscx100\\fscy100)}}"
    else:
        pop_tag = ""

    if word_by_word:
        # Plain white, not the karaoke highlight colour — that's the actual
        # MrBeast look: white text, thick dark outline, no per-word colour.
        events = _build_word_pop_events(words, s, primary_ovr, pop_tag, delay_sec)
        return header + "\n".join(events) + "\n"

    events: list[str] = []
    for line in _group_lines(words, s["max_words_per_line"]):
        line_start = line[0].start
        line_end = line[-1].end
        for i, active in enumerate(line):
            parts = []
            for j, w in enumerate(line):
                text = w.text.upper() if s["all_caps"] else w.text
                if j == i:
                    parts.append(f"{highlight_ovr}{text}")
                else:
                    parts.append(f"{primary_ovr}{text}")
            payload = pop_tag + " ".join(parts)
            start = max(line_start, active.start) + delay_sec
            # Karaoke: hold each word's highlight until the next word begins
            # so the line never disappears between words.
            if i + 1 < len(line):
                end = min(line_end, line[i + 1].start) + delay_sec
                end = max(end, active.end + delay_sec)  # never shorter than the word itself
            else:
                end = min(line_end, active.end) + delay_sec
            if end <= start:
                continue
            events.append(
                f"Dialogue: 0,{_pct_to_ass_time(start)},"
                f"{_pct_to_ass_time(end)},Default,,0,0,0,,{payload}"
            )

    return header + "\n".join(events) + "\n"


def apply_cuts_to_words(words: list[Word], cuts: list[dict],
                         duration: float) -> list[Word]:
    """Remove words inside cut sections and shift remaining timestamps."""
    segs: list[tuple[float, float]] = []
    cur = 0.0
    for c in sorted(cuts, key=lambda x: x["start"]):
        s, e = float(c["start"]), float(c["end"])
        if s > cur + 0.01:
            segs.append((cur, s))
        cur = max(cur, e)
    if cur < duration - 0.01:
        segs.append((cur, duration))
    if not segs:
        return []

    result: list[Word] = []
    offset = 0.0
    for seg_start, seg_end in segs:
        for w in words:
            if w.start >= seg_start and w.end <= seg_end:
                result.append(Word(
                    start=w.start - seg_start + offset,
                    end=w.end - seg_start + offset,
                    text=w.text,
                ))
        offset += seg_end - seg_start
    return result


def write_ass(words: list[Word], cfg: dict,
              video_size: tuple[int, int], out_path: Path,
              delay_sec: float = 0.0) -> Path:
    out_path.write_text(build_ass(words, cfg, video_size, delay_sec), encoding="utf-8")
    return out_path
