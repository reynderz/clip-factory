"""Pick viral reaction emojis from chat near a peak moment and render as PNG.

Counts all emojis posted in chat during [peak - window_before, peak + window_after].
Falls back to a reason-based default emoji when chat has none:
  chat_read moment → 💀   audio peak → 🔥   anything else → 😂
"""
from __future__ import annotations
import re
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"    # misc symbols & pictographs
    "\U0001F680-\U0001F6FF"    # transport & map
    "\U0001F1E0-\U0001F1FF"    # regional indicator letters (flags)
    "\U00002600-\U000027BF"    # misc symbols
    "\U0001F900-\U0001F9FF"    # supplemental symbols & pictographs
    "\U0001FA00-\U0001FAFF"    # symbols & pictographs extended-A/B
    "\U00002702-\U000027B0"    # dingbats
    "]+",
    flags=re.UNICODE,
)
_APPLE_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
_REASON_FALLBACK = {"chat_read": "💀", "audio": "🔥"}
_DEFAULT_FALLBACK = "😂"


def pick_emojis(chat_msgs, peak_sec: float, reasons: list[str],
                window_before: float = 15.0, window_after: float = 5.0,
                max_emojis: int = 3) -> list[str]:
    """Return up to max_emojis most-used emojis from chat near peak_sec."""
    found: list[str] = []
    for msg in chat_msgs:
        if not (peak_sec - window_before <= msg.offset_sec <= peak_sec + window_after):
            continue
        for match in _EMOJI_RE.findall(msg.text):
            found.extend(ch for ch in match if ord(ch) > 0x25FF)

    if not found:
        fb = next(
            (_REASON_FALLBACK[k] for k in _REASON_FALLBACK
             if any(k in r for r in reasons)),
            _DEFAULT_FALLBACK,
        )
        return [fb]

    return [e for e, _ in Counter(found).most_common(max_emojis)]


def render_emoji_png(emojis: list[str], font_size: int, out_path: Path) -> Path:
    """Render the emoji list as a transparent PNG."""
    text = "  ".join(emojis)
    try:
        font = ImageFont.truetype(_APPLE_EMOJI, font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()

    probe = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font)
    pad = 12
    w = max(bbox[2] - bbox[0] + pad * 2, 1)
    h = max(bbox[3] - bbox[1] + pad * 2, 1)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (pad - bbox[0], pad - bbox[1]),
        text, font=font, embedded_color=True,
    )
    img.save(out_path, "PNG")
    return out_path
