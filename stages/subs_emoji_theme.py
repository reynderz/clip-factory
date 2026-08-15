"""MrBeast-style random emoji pops beside the spoken word.

Roughly every Nth spoken word gets a random emoji rendered as its own
transparent PNG (Apple Color Emoji via Pillow, same technique as
title_overlay.py / emoji_overlay.py — libass can't reliably draw colour
emoji glyphs itself). render.py composites each PNG with a bell-curve scale
"pop" and a quick alpha fade.

In word_pop style, the word is centered and shown alone, so we can measure
its rendered width (same font/size libass would use) and place the emoji
just to its right, inline — not floating in a separate row above. Karaoke
style shows a multi-word line where the active word's on-screen position
isn't known without re-implementing libass's line layout, so that case falls
back to floating above the caption, centered with a little jitter.
"""
from __future__ import annotations
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .transcribe import Word
from .emoji_font import load_apple_emoji_font
from .subtitle import effective_font_size, get_style

# One overlay tuple: (png_path, start_sec, duration_sec, x_offset_frac).
# x_offset_frac shifts the emoji right (positive) or left (negative) of
# frame-center, as a fraction of frame width.
EmojiPop = tuple[Path, float, float, float]

_KOMIKA_FONT_PATH = Path.home() / "Library" / "Fonts" / "KOMIKAX_.ttf"


def _load_text_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(_KOMIKA_FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _word_pixel_width(text: str, subs_cfg: dict) -> float:
    """Approximate on-screen width of `text` in the word_pop ASS style."""
    font = _load_text_font(effective_font_size(subs_cfg))
    display = text.upper() if subs_cfg.get("all_caps") else text
    probe = Image.new("RGBA", (1, 1))
    return ImageDraw.Draw(probe).textlength(display, font=font)

# Words that map to an obviously-matching emoji instead of a random pool pick.
# Dutch entries first (Whisper transcribes in whatever language is spoken —
# these streams are Dutch), English loanwords kept too since streamers mix them in.
_WORD_EMOJI: dict[str, str] = {
    # 🔥 hype / sick / awesome
    "vuur": "🔥", "vet": "🔥", "waanzinnig": "🔥", "episch": "🔥",
    "keihard": "🔥", "sick": "🔥",
    "fire": "🔥", "lit": "🔥", "insane": "🔥", "goated": "🔥",
    "cracked": "🔥", "heat": "🔥", "blazing": "🔥",
    # 💀 dead / dying (laughing)
    "dood": "💀", "kapot": "💀", "doodgaan": "💀", "doodlach": "💀",
    "dead": "💀", "deadass": "💀", "lmao": "💀", "lmfao": "💀",
    "died": "💀", "dying": "💀",
    # 🤯 mind blown / crazy / unbelievable
    "gek": "🤯", "belachelijk": "🤯", "ongelofelijk": "🤯", "waanzin": "🤯",
    "crazy": "🤯", "wild": "🤯", "unbelievable": "🤯", "wtf": "🤯",
    "mindblowing": "🤯",
    # 😂 funny
    "grappig": "😂", "hilarisch": "😂", "lachen": "😂", "hahaha": "😂",
    "funny": "😂", "hilarious": "😂", "lol": "😂", "haha": "😂", "lmaooo": "😂",
    # 👀 look / watch
    "kijk": "👀", "kijken": "👀", "checken": "👀",
    "look": "👀", "watch": "👀", "looking": "👀", "watching": "👀",
    # 💯 perfect / facts
    "precies": "💯", "klopt": "💯",
    "perfect": "💯", "exactly": "💯", "facts": "💯",
    # 😱 scared / scary
    "eng": "😱", "bang": "😱", "griezelig": "😱", "schrikken": "😱",
    "scared": "😱", "terrifying": "😱", "scary": "😱", "screaming": "😱",
    # 😳 seriously (NOT "wat"/"echt"/"what"/"really" — too common in normal
    # Dutch/English speech, would trigger on nearly every sentence)
    "serieus": "😳", "seriously": "😳",
    # ⚡ fast
    "snel": "⚡", "supersnel": "⚡", "razendsnel": "⚡",
    "fast": "⚡", "quick": "⚡", "instantly": "⚡", "speed": "⚡",
    # 🚨 clutch / close call
    "nipt": "🚨", "clutch": "🚨", "close": "🚨",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower())


def _render_emoji_png(emoji: str, size: int, out_path: Path) -> Path:
    try:
        font, strike = load_apple_emoji_font(size)
    except (OSError, IOError):
        try:
            font, strike = ImageFont.load_default(size=size), size
        except TypeError:
            font, strike = ImageFont.load_default(), size

    probe = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), emoji, font=font)
    pad = max(8, strike // 16)
    w = max(bbox[2] - bbox[0] + pad * 2, 1)
    h = max(bbox[3] - bbox[1] + pad * 2, 1)

    glyph = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glyph).text(
        (pad - bbox[0], pad - bbox[1]),
        emoji, font=font, embedded_color=True,
    )

    # A flat emoji glyph pasted directly on video reads as a cheap sticker —
    # a soft dark contact shadow gives it depth and keeps it legible over any
    # background, the same job the subtitle text's outline/shadow does.
    shadow_offset = max(2, strike // 32)
    blur_radius = max(2, strike // 24)
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(glyph.split()[3].point(lambda a: int(a * 0.55)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur_radius))

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.alpha_composite(shadow, (0, shadow_offset))
    img.alpha_composite(glyph, (0, 0))

    if strike != size:
        scale = size / strike
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                          Image.LANCZOS)
    img.save(out_path, "PNG")
    return out_path


def pick_pop_words(words: list[Word], every_n_words: int,
                    seed: int) -> list[Word]:
    """Roughly every Nth word, jittered so pops don't land on a rigid grid."""
    if every_n_words <= 0 or not words:
        return []
    rng = random.Random(seed)
    picked: list[Word] = []
    i = 0
    while i < len(words):
        picked.append(words[i])
        step = every_n_words + rng.randint(-1, 1)
        i += max(1, step)
    return picked


def build_emoji_theme_overlays(words: list[Word], cfg: dict, work: Path,
                                idx: int, name: str,
                                delay_sec: float = 0.0) -> list[EmojiPop]:
    """Render pop PNGs for this clip/variant and return overlay placements."""
    ec = cfg.get("subs_emoji_theme", {})
    if not ec.get("enabled", False):
        return []

    pool = ec.get("pool") or ["🔥", "💀", "😂"]
    size = int(ec.get("size", 140))
    display_sec = float(ec.get("display_sec", 0.6))
    every_n = int(ec.get("every_n_words", 4))
    min_gap = display_sec * 0.8   # keep two pops from stacking on the same spot

    subs_cfg = cfg.get("subs", {})
    is_word_pop = get_style(subs_cfg) == "word_pop"
    ow = int(cfg.get("output", {}).get("resolution", [1080, 1920])[0])
    gap_px = float(ec.get("gap_px", 24))
    x_jitter = float(ec.get("fallback_x_jitter_frac", 0.10))

    seed = idx * 7919 + sum(ord(c) for c in name)
    rng = random.Random(seed)

    # 1) Words that obviously match an emoji always get that one — the theme
    #    should track what's actually being said, not just fire at random.
    picks: list[tuple[Word, str]] = []
    for w in words:
        emoji = _WORD_EMOJI.get(_normalize(w.text))
        if emoji:
            picks.append((w, emoji))

    # 2) Fill the gaps with the every-N-words rhythm (random from the pool)
    #    so clips with no keyword hits still get the MrBeast pop cadence.
    covered = {id(w) for w, _ in picks}
    for w in pick_pop_words(words, every_n, seed):
        if id(w) in covered:
            continue
        if any(abs(w.start - pw.start) < min_gap for pw, _ in picks):
            continue
        picks.append((w, rng.choice(pool)))

    picks.sort(key=lambda p: p[0].start)

    # Cache rendered emoji PNGs by character — most clips reuse a handful.
    png_cache: dict[str, Path] = {}
    overlays: list[EmojiPop] = []
    for w, emoji in picks:
        png_path = png_cache.get(emoji)
        if png_path is None:
            png_path = work / f"clip_{idx:03d}__{name}_emojipop_{abs(hash(emoji)) % 10000}.png"
            _render_emoji_png(emoji, size, png_path)
            png_cache[emoji] = png_path
        # Clamp AFTER applying delay_sec (which is usually negative, e.g.
        # -0.15s) — ffmpeg's fade filter rejects a negative `st`, and
        # clamping before adding the delay doesn't stop it going negative.
        start_t = max(0.0, w.start - 0.05 + delay_sec)
        if is_word_pop:
            half_word_px = _word_pixel_width(w.text, subs_cfg) / 2
            x_offset = min((half_word_px + gap_px) / ow, 0.42)
        else:
            x_offset = rng.uniform(-x_jitter, x_jitter)
        overlays.append((png_path, start_t, display_sec, x_offset))
    return overlays
