"""Render a clip title (with emoji) as a transparent PNG using Pillow.

Uses Apple Color Emoji for emoji characters and the subtitle font for the
rest, so colour emoji work reliably regardless of libass version.
"""
from __future__ import annotations
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .emoji_font import load_apple_emoji_font

_EMOJI_RE = re.compile(
    u'[\U0001F300-\U0001FAFF'
    u'\U00002600-\U000027BF'
    u'\U0000FE00-\U0000FE0F'
    u'\U00002300-\U000023FF'
    u'\U00002B50-\U00002BFF'
    u']+'
)


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
        emoji_font, emoji_strike = load_apple_emoji_font(font_size)
        emoji_scale = font_size / emoji_strike
    except OSError:
        emoji_font, emoji_scale = main_font, 1.0

    segments = _split_segments(text)

    # Measure total rendered width on a scratch canvas. Emoji are measured at
    # their native strike size then scaled to the effective on-canvas width.
    scratch = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(scratch)

    def _seg_width(seg: str, is_emoji: bool) -> int:
        w = d.textlength(seg, font=(emoji_font if is_emoji else main_font))
        return int(w * emoji_scale) if is_emoji else int(w)

    total_w = sum(_seg_width(seg, is_emoji) for seg, is_emoji in segments)

    ascent, descent = main_font.getmetrics()
    pad = outline + 10
    img_w = max(1, total_w + pad * 2)
    img_h = max(1, ascent + descent + pad * 2)

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x, y = pad, pad
    for seg, is_emoji in segments:
        if is_emoji:
            # Render at the font's native (valid) strike size on its own tile,
            # then resize the bitmap to the requested font_size before pasting
            # — Apple Color Emoji only has fixed embedded sizes.
            bbox = d.textbbox((0, 0), seg, font=emoji_font)
            tile_w = max(1, bbox[2] - bbox[0])
            tile_h = max(1, bbox[3] - bbox[1])
            tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((-bbox[0], -bbox[1]), seg, font=emoji_font,
                                       embedded_color=True)
            if emoji_scale != 1.0:
                tile = tile.resize((max(1, round(tile_w * emoji_scale)),
                                     max(1, round(tile_h * emoji_scale))),
                                    Image.LANCZOS)
            img.paste(tile, (x, y), tile)
        else:
            draw.text(
                (x, y), seg, font=main_font,
                fill=(255, 255, 255, 255),
                stroke_width=outline,
                stroke_fill=(0, 0, 0, 255),
            )
        x += _seg_width(seg, is_emoji)

    img.save(out_path)
    return out_path
