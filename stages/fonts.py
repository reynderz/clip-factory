"""Cross-platform lookup for the user-installed Komika Axis font.

The font isn't bundled with the repo (license) — the README asks the user
to install it system-wide. Where "system-wide" means differs per OS.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path


def komika_font_path() -> Path:
    """Best-guess path to KOMIKAX_.ttf for the current OS."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "Microsoft" / "Windows" / "Fonts" / "KOMIKAX_.ttf"
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts" / "KOMIKAX_.ttf"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Fonts" / "KOMIKAX_.ttf"
    return Path.home() / ".fonts" / "KOMIKAX_.ttf"
