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


def build_ass(words: list[Word], cfg: dict, video_size: tuple[int, int]) -> str:
    s = cfg["subs"]
    vw, vh = video_size

    primary = s["primary_color"]
    highlight = s["highlight_color"]
    outline = s["outline_color"]

    margin_v = int(vh * (1.0 - s["position_y_frac"]))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{s["font"]},{s["font_size"]},{primary},{primary},{outline},&H00000000,-1,0,0,0,100,100,0,0,1,{s["outline"]},{s["shadow"]},2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []
    primary_ovr = _ass_color_override(primary)
    highlight_ovr = _ass_color_override(highlight)

    for line in _group_lines(words, s["max_words_per_line"]):
        line_start = line[0].start
        line_end = line[-1].end
        # For each word, emit one event spanning that word's duration,
        # showing the whole line with the active word colored.
        for i, active in enumerate(line):
            parts = []
            for j, w in enumerate(line):
                text = w.text.upper() if s["all_caps"] else w.text
                if j == i:
                    parts.append(f"{highlight_ovr}{text}")
                else:
                    parts.append(f"{primary_ovr}{text}")
            payload = " ".join(parts)
            start = max(line_start, active.start)
            end = min(line_end, active.end)
            if end <= start:
                continue
            events.append(
                f"Dialogue: 0,{_pct_to_ass_time(start)},"
                f"{_pct_to_ass_time(end)},Default,,0,0,0,,{payload}"
            )

    return header + "\n".join(events) + "\n"


def write_ass(words: list[Word], cfg: dict,
              video_size: tuple[int, int], out_path: Path) -> Path:
    out_path.write_text(build_ass(words, cfg, video_size), encoding="utf-8")
    return out_path
