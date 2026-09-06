"""Shared loader for the system colour emoji font.

Apple Color Emoji.ttc is a bitmap font with a fixed set of embedded pixel
sizes ("strikes") — requesting an arbitrary size raises `OSError: invalid
pixel size` and Pillow has no fallback of its own, so callers silently end
up drawing tofu boxes instead of colour glyphs. This loads the nearest
available strike; callers resize the rendered bitmap to the size they
actually wanted.

Windows' Segoe UI Emoji and Linux's Noto Color Emoji are scalable colour
fonts, so they load directly at the requested size — strike_size just comes
back equal to target_size and callers skip the resize step.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

from PIL import ImageFont

APPLE_EMOJI_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"
WINDOWS_EMOJI_PATH = str(
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts" / "seguiemj.ttf"
)
LINUX_EMOJI_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
]

# Embedded bitmap strikes available in the system font (macOS 14/15/26).
_STRIKE_SIZES = [20, 32, 40, 48, 64, 96, 160]


def load_apple_emoji_font(target_size: int) -> tuple[ImageFont.FreeTypeFont, int]:
    """Return (font, strike_size) for the platform's colour emoji font.

    On macOS, strike_size is the nearest embedded bitmap strike >= target_size
    (or the largest strike if target_size exceeds all of them) — callers
    resize the rendered bitmap by target_size / strike_size. On Windows/Linux
    the font is scalable, so strike_size == target_size and no resize is needed.
    """
    if sys.platform == "darwin":
        strike = next((sz for sz in _STRIKE_SIZES if sz >= target_size), _STRIKE_SIZES[-1])
        return ImageFont.truetype(APPLE_EMOJI_PATH, strike, index=0), strike
    if sys.platform == "win32":
        return ImageFont.truetype(WINDOWS_EMOJI_PATH, target_size), target_size
    for path in LINUX_EMOJI_CANDIDATES:
        try:
            return ImageFont.truetype(path, target_size), target_size
        except OSError:
            continue
    raise OSError("No colour emoji font found on this system")
