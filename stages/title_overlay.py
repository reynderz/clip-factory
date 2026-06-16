"""Render a clip title (with emoji) as a transparent PNG using Pillow.

Uses Apple Color Emoji for emoji characters and the subtitle font for the
rest, so colour emoji work reliably regardless of libass version.
"""
from __future__ import annotations
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_EMOJI_RE = re.compile(
    u'[\U0001F300-\U0001FAFF'
    u'\U00002600-\U000027BF'
    u'\U0000FE00-\U0000FE0F'
    u'\U00002300-\U000023FF'
    u'\U00002B50-\U00002BFF'
    u']+'
)

_EMOJI_FONT_PATH = Path("/System/Library/Fonts/Apple Color Emoji.ttc")


def _split_segments(text: str) -> list[tuple[str, bool]]:
    parts = _EMOJI_RE.split(text)
    emojis = _EMOJI_RE.findall(text)
    result = []
    for i, part in enumerate(parts):
        if part:
            result.append((part, False))
        if i < len(emojis):
            result.append((emojis[i], True))
    return result


def render_title_png(title: str, cfg: dict, out_path: Path) -> Path:
    """Write a transparent PNG of the title text, with emoji rendered in colour."""
    s = cfg["subs"]
    font_size = int(s["font_size"])
    outline = int(s.get("outline", 5))
    text = title.upper() if s.get("all_caps") else title

    # Load fonts
    komika = Path.home() / "Library" / "Fonts" / "KOMIKAX_.ttf"
    try:
        main_font = ImageFont.truetype(str(komika), font_size)
    except OSError:
        main_font = ImageFont.load_default(size=font_size)

    try:
        emoji_font = ImageFont.truetype(str(_EMOJI_FONT_PATH), font_size, index=0)
    except OSError:
        emoji_font = main_font

    segments = _split_segments(text)

    # Measure total rendered width on a scratch canvas
    scratch = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(scratch)
    total_w = sum(
        int(d.textlength(seg, font=(emoji_font if is_emoji else main_font)))
        for seg, is_emoji in segments
    )

    ascent, descent = main_font.getmetrics()
    pad = outline + 10
    img_w = max(1, total_w + pad * 2)
    img_h = max(1, ascent + descent + pad * 2)

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x, y = pad, pad
    for seg, is_emoji in segments:
        font = emoji_font if is_emoji else main_font
        draw.text(
            (x, y), seg, font=font,
            fill=(255, 255, 255, 255),
            stroke_width=0 if is_emoji else outline,
            stroke_fill=(0, 0, 0, 255),
        )
        x += int(d.textlength(seg, font=font))

    img.save(out_path)
    return out_path
