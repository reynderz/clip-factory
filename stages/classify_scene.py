"""Detect which OBS scene the streamer is using at a given moment.

Scene A (Gaming):      chat sidebar LEFT,  game center,          webcam bottom-right.
Scene B (Just Chatting): face webcam LEFT, chat sidebar RIGHT.
Scene C (Watch Party): chat sidebar LEFT,  video/anime center,   webcam top-right.

Two-region heuristic
--------------------
LEFT region  = scene_a's obs_chat_region (x 0–0.31).
RIGHT region = scene_c's webcam box      (x 0.77, y 0.13, w 0.23, h 0.45).

Decision tree:
  left_density < scene_a_chat_edge_min  →  scene_b  (no chat sidebar on left)
  left_density high + right_density < scene_c_right_edge_max
                                        →  scene_c  (face in right box = smooth)
  left_density high + right_density high→  scene_a  (game/content fills right)

Both thresholds are tunable via calibrate.py.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import cv2
import numpy as np


def _grab_frame(vod: Path, ts: float) -> np.ndarray:
    cmd = [
        "ffmpeg", "-ss", f"{ts:.3f}", "-i", str(vod),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Could not decode frame at {ts}s")
    return img


def _edge_density(img: np.ndarray, box: dict) -> float:
    h, w = img.shape[:2]
    x = int(box["x"] * w); y = int(box["y"] * h)
    bw = int(box["w"] * w); bh = int(box["h"] * h)
    crop = img[y:y + bh, x:x + bw]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 100, 200)
    return float(np.mean(edges > 0))


def classify_scene(vod: Path, start: float, end: float,
                   cfg: dict) -> str:
    """Return 'scene_a', 'scene_b', or 'scene_c' for the given clip window.

    Uses the edge-density difference between the left chat region and the
    right chat region to determine which side the chat sidebar is on:
      right − left > scene_b_right_lead   →  scene_b  (chat on right)
      left  > scene_c_left_min
        AND right > scene_a_right_min     →  scene_a  (both rich = gaming)
      left  > scene_c_left_min            →  scene_c  (chat left, right quiet = watch party)
      default                             →  scene_b
    """
    sc          = cfg["scene"]
    n           = sc["sample_frames"]
    right_lead  = sc.get("scene_b_right_lead", 0.010)
    left_min    = sc.get("scene_c_left_min",   0.025)
    right_min   = sc.get("scene_a_right_min",  0.025)

    left_box  = cfg["layout"]["scenes"]["scene_a"]["obs_chat_region"]
    right_box = cfg["layout"]["scenes"]["scene_b"]["obs_chat_region"]

    timestamps = np.linspace(start, end, n + 2)[1:-1]
    left_densities:  list[float] = []
    right_densities: list[float] = []

    for ts in timestamps:
        try:
            img = _grab_frame(vod, float(ts))
            left_densities.append(_edge_density(img, left_box))
            right_densities.append(_edge_density(img, right_box))
        except Exception:
            continue

    if not left_densities:
        return "scene_b"

    left_median  = float(np.median(left_densities))
    right_median = float(np.median(right_densities))

    if right_median - left_median > right_lead:
        return "scene_b"                 # chat clearly dominates on right
    if left_median > left_min and right_median > right_min:
        return "scene_a"                 # both rich → gaming
    if left_median > left_min:
        return "scene_c"                 # chat left, right quiet → watch party
    return "scene_b"                     # no clear chat sidebar
