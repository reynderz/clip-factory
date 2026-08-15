"""Shared loader for Apple Color Emoji.

Apple Color Emoji.ttc is a bitmap font with a fixed set of embedded pixel
sizes ("strikes") — requesting an arbitrary size raises `OSError: invalid
pixel size` and Pillow has no fallback of its own, so callers silently end
up drawing tofu boxes instead of colour glyphs. This loads the nearest
available strike; callers resize the rendered bitmap to the size they
actually wanted.
"""
from __future__ import annotations
from PIL import ImageFont

APPLE_EMOJI_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"

# Embedded bitmap strikes available in the system font (macOS 14/15/26).
_STRIKE_SIZES = [20, 32, 40, 48, 64, 96, 160]


def load_apple_emoji_font(target_size: int) -> tuple[ImageFont.FreeTypeFont, int]:
    """Return (font, strike_size) for the smallest strike >= target_size,
    or the largest strike if target_size exceeds all of them.

    strike_size will often differ from target_size — resize the rendered
    bitmap by target_size / strike_size to get the requested display size.
    """
    strike = next((sz for sz in _STRIKE_SIZES if sz >= target_size), _STRIKE_SIZES[-1])
    return ImageFont.truetype(APPLE_EMOJI_PATH, strike, index=0), strike
