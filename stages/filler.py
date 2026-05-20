"""Pick a filler video and a start offset for a brainrot clip.

Per-channel config:
    brainrot:
      enabled: true
      filler_dir: "fillers/parkour"   # relative to project root
      swap: false                     # if true, filler on top
      webcam_top_frac: 0.5

The picker:
  - lists all .mp4/.mov/.mkv files in filler_dir
  - picks one at random (seeded by clip index for reproducibility if wanted)
  - probes its duration with ffprobe
  - picks a random start offset so the clip window fits inside the filler
    length; if the filler is shorter than the clip, offset=0 and ffmpeg
    will loop it (via -stream_loop -1).
"""
from __future__ import annotations
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path


FILLER_EXTS = {".mp4", ".mov", ".mkv", ".webm"}


@dataclass
class FillerPick:
    path: Path
    start_sec: float       # offset into the filler where we begin
    needs_loop: bool       # filler shorter than clip → use -stream_loop -1


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out) if out else 0.0


def pick_filler(filler_dir: Path, clip_duration: float,
                seed: int | None = None) -> FillerPick | None:
    if not filler_dir.exists():
        return None
    files = [p for p in filler_dir.iterdir()
             if p.suffix.lower() in FILLER_EXTS]
    if not files:
        return None

    rng = random.Random(seed)
    # Try a few files; prefer ones long enough that we can pick a non-zero offset
    rng.shuffle(files)
    for f in files:
        dur = _probe_duration(f)
        if dur <= 0:
            continue
        if dur >= clip_duration + 1:
            # pick a random start so each render feels fresh
            max_start = max(0.0, dur - clip_duration - 1)
            return FillerPick(path=f,
                              start_sec=rng.uniform(0, max_start),
                              needs_loop=False)
    # Fall back: filler is shorter than clip, loop it
    f = files[0]
    return FillerPick(path=f, start_sec=0.0, needs_loop=True)
