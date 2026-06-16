"""Render a Twitch-style chat message as a transparent PNG.

Visual design matches Twitch's chat UI:
  • Very dark background (#0E0E10) with a Twitch-purple (#9147FF) left accent bar
  • Username in the user's Twitch colour (bold)
  • Colon in muted grey
  • Message in Twitch near-white (#EFEFF1)
  • Clean sans-serif font (Arial Bold / system fallback)
"""
from __future__ import annotations
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Twitch's default username palette (colours assigned when user hasn't chosen one)
_TWITCH_COLORS = [
    "#FF0000", "#0000FF", "#008000", "#B22222", "#FF7F50",
    "#9ACD32", "#FF4500", "#2E8B57", "#DAA520", "#D2691E",
    "#5F9EA0", "#1E90FF", "#FF69B4", "#8A2BE2", "#00FF7F",
]

# Twitch UI constants
_TWITCH_PURPLE  = (145, 71, 255, 255)   # #9147FF  – left accent bar
_BG_COLOR       = (14,  14,  16, 240)   # #0E0E10  – Twitch dark background
_COLON_COLOR    = (173, 173, 184, 255)  # #ADADB8  – muted colon
_MSG_COLOR      = (239, 239, 241, 255)  # #EFEFF1  – message text
_BAR_W          = 8                      # width of the left purple bar (px)
_RADIUS         = 14                     # corner radius


def _user_color_rgb(username: str) -> tuple[int, int, int, int]:
    h = hashlib.md5(username.encode("utf-8")).digest()
    hex_col = _TWITCH_COLORS[h[0] % len(_TWITCH_COLORS)].lstrip("#")
    r, g, b = int(hex_col[0:2], 16), int(hex_col[2:4], 16), int(hex_col[4:6], 16)
    return (r, g, b, 255)


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a clean sans-serif font suitable for Twitch-style chat."""
    home = Path.home()
    bold_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        str(home / "Library" / "Fonts" / "Arial Bold.ttf"),
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    regular_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        str(home / "Library" / "Fonts" / "Arial.ttf"),
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in (bold_candidates if bold else regular_candidates):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str,
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
                        font_name: str = "Komika Axis",   # kept for API compat, ignored
                        font_size: int = 44,
                        padding: int = 24,
                        max_width_frac: float = 0.88) -> Path:
    """Write a transparent PNG of a Twitch-style chat message."""

    # Clamp font size to something readable but not oversized for chat UI
    chat_size = max(28, min(font_size, 52))
    name_font = _load_font(chat_size, bold=True)
    msg_font  = _load_font(chat_size, bold=False)

    max_box_w  = int(frame_w * max_width_frac)
    text_x0    = _BAR_W + padding          # left edge of text
    max_text_w = max_box_w - text_x0 - padding

    scratch = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(scratch)

    user_disp = username + ":"
    colon_disp = ":"
    name_only  = username
    user_w     = d.textlength(user_disp + "  ", font=name_font)

    # Fit as many message words as possible on the first line after the username
    first_line_budget = max(0, int(max_text_w - user_w))
    words = message.split()
    first_words, rest_words = [], []
    cur_w = 0
    for i, w in enumerate(words):
        ww = d.textlength(w + " ", font=msg_font)
        if cur_w + ww <= first_line_budget:
            first_words.append(w)
            cur_w += ww
        else:
            rest_words = words[i:]
            break

    rest_lines = _wrap(d, " ".join(rest_words), msg_font, max_text_w) if rest_words else []

    ascent_n, descent_n = name_font.getmetrics()
    ascent_m, descent_m = msg_font.getmetrics()
    # Use the taller of the two fonts for line height
    line_h = max(ascent_n + descent_n, ascent_m + descent_m) + 6

    total_lines = 1 + len(rest_lines)
    box_h = padding * 2 + total_lines * line_h
    box_w = max_box_w

    img  = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background (dark Twitch card) ──────────────────────────────
    draw.rounded_rectangle([(0, 0), (box_w - 1, box_h - 1)],
                           radius=_RADIUS, fill=_BG_COLOR)

    # ── Left purple accent bar ─────────────────────────────────────
    # Draw it as a filled rectangle clipped to the rounded card corners
    inner_bar_h = box_h - _RADIUS * 2
    draw.rectangle([(_RADIUS, _RADIUS),
                    (_BAR_W + _RADIUS, _RADIUS + inner_bar_h)],
                   fill=_TWITCH_PURPLE)
    # Fill top and bottom of bar (where radius cut it off) with a smaller rect
    draw.rounded_rectangle([(0, 0), (_BAR_W + _RADIUS, box_h - 1)],
                           radius=_RADIUS, fill=_TWITCH_PURPLE)
    # Mask back the right side of the bar so it stays within BAR_W
    draw.rectangle([(_BAR_W, 0), (_BAR_W + _RADIUS, box_h - 1)], fill=_BG_COLOR)

    # ── Row 1: username + colon + first words of message ──────────
    y = padding
    x = text_x0

    name_w = d.textlength(name_only, font=name_font)
    draw.text((x, y), name_only, font=name_font, fill=_user_color_rgb(username))
    x += int(name_w)

    colon_w = d.textlength(colon_disp, font=name_font)
    draw.text((x, y), colon_disp, font=name_font, fill=_COLON_COLOR)
    x += int(colon_w) + int(d.textlength(" ", font=msg_font))

    if first_words:
        draw.text((x, y), " ".join(first_words), font=msg_font, fill=_MSG_COLOR)

    # ── Continuation lines ─────────────────────────────────────────
    for i, line in enumerate(rest_lines, 1):
        draw.text((text_x0, padding + i * line_h), line,
                  font=msg_font, fill=_MSG_COLOR)

    img.save(out_path)
    return out_path
