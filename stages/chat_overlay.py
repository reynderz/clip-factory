"""Render a Twitch-style chat message as a transparent PNG.

Style: dark translucent rounded box, colored username, white message.
ffmpeg then overlays this onto the clip at the configured position.

Username colors: Twitch assigns deterministic colors per user when they
haven't picked one. We pick from the default Twitch palette based on a
hash of the username, which looks authentic.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Twitch's default username palette
_TWITCH_COLORS = [
    "#FF0000", "#0000FF", "#008000", "#B22222", "#FF7F50",
    "#9ACD32", "#FF4500", "#2E8B57", "#DAA520", "#D2691E",
    "#5F9EA0", "#1E90FF", "#FF69B4", "#8A2BE2", "#00FF7F",
]


def _user_color(username: str) -> str:
    h = hashlib.md5(username.encode("utf-8")).digest()
    return _TWITCH_COLORS[h[0] % len(_TWITCH_COLORS)]


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Best-effort font loading with system-font fallback."""
    candidates = [
        name,
        f"{name}.ttf",
        f"/System/Library/Fonts/Supplemental/{name}.ttf",  # macOS
        f"/Library/Fonts/{name}.ttf",
        "/System/Library/Fonts/Helvetica.ttc",             # macOS fallback
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str,
               font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_chat_overlay(username: str, message: str,
                        out_path: Path,
                        frame_w: int = 1080,
                        font_name: str = "Komika Axis",
                        font_size: int = 44,
                        padding: int = 28,
                        max_width_frac: float = 0.90) -> Path:
    """Writes a transparent PNG of the Twitch-style message."""
    max_box_w = int(frame_w * max_width_frac)
    # inner wrap width = box minus padding on both sides
    max_text_w = max_box_w - 2 * padding

    font = _load_font(font_name, font_size)
    username_text = f"{username}:"

    # Measure on a scratch canvas
    scratch = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(scratch)

    # Wrap the message. Username always fits on the first line if possible.
    user_w = d.textlength(username_text + " ", font=font)
    first_line_budget = int(max_text_w - user_w)

    # Greedy first-line fit, then wrap the rest.
    words = message.split()
    first_line, rest = [], []
    cur_w = 0
    for i, w in enumerate(words):
        w_width = d.textlength(w + " ", font=font)
        if cur_w + w_width <= first_line_budget:
            first_line.append(w)
            cur_w += w_width
        else:
            rest = words[i:]
            break
    rest_lines = _wrap_text(d, " ".join(rest), font, max_text_w) if rest else []

    # Compute line height from font ascent/descent
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 6

    total_lines = 1 + len(rest_lines)
    box_h = padding * 2 + total_lines * line_h
    box_w = max_box_w

    # Actually draw
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Rounded dark-translucent background
    draw.rounded_rectangle(
        [(0, 0), (box_w - 1, box_h - 1)],
        radius=22,
        fill=(15, 15, 20, 220),
    )

    # First line: colored username + start of message
    y = padding
    x = padding
    draw.text((x, y), username_text, font=font, fill=_user_color(username))
    x += int(d.textlength(username_text + " ", font=font))
    if first_line:
        draw.text((x, y), " ".join(first_line), font=font,
                  fill=(255, 255, 255, 255))
    # Remaining wrapped lines
    for i, line in enumerate(rest_lines, 1):
        draw.text((padding, padding + i * line_h), line, font=font,
                  fill=(255, 255, 255, 255))

    img.save(out_path)
    return out_path
