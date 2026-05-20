"""Calibrate scene-detection thresholds.

Scene A = Gaming      (chat sidebar left, game center, webcam bottom-right).
Scene B = Just Chatting (full-frame webcam, chat baked into right side).
Scene C = Watch Party (chat sidebar left, video/anime center, webcam top-right).

Two thresholds are calibrated:
  scene_a_chat_edge_min  — LEFT region;  separates B (low) from A/C (high).
  scene_c_right_edge_max — RIGHT region; separates C (face = low) from A (game = high).

Usage (all three scenes):
    python calibrate.py work/<vod_id>/<vod_id>.mp4 \\
        --scene-a 340 920 1500 \\
        --scene-b 60 180 2400 \\
        --scene-c 600 1200 3000

Usage (A vs B only):
    python calibrate.py work/<vod_id>/<vod_id>.mp4 \\
        --scene-a 340 920 1500 \\
        --scene-b 60 180 2400
"""
from __future__ import annotations
import argparse
from pathlib import Path

from stages.classify_scene import _grab_frame, _edge_density
from stages.cfg import load_global


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vod")
    ap.add_argument("--scene-a", type=float, nargs="+", required=True,
                    help="timestamps (sec) of clear Scene A (gaming) moments")
    ap.add_argument("--scene-b", type=float, nargs="+", required=True,
                    help="timestamps (sec) of clear Scene B (just chatting) moments")
    ap.add_argument("--scene-c", type=float, nargs="*", default=[],
                    help="timestamps (sec) of clear Scene C (watch party) moments")
    args = ap.parse_args()

    cfg       = load_global()
    left_box  = cfg["layout"]["scenes"]["scene_a"]["obs_chat_region"]
    right_box = cfg["layout"]["scenes"]["scene_b"]["obs_chat_region"]

    def measure_both(label, timestamps):
        print(f"\n{label}:")
        lefts, rights = [], []
        for ts in timestamps:
            img = _grab_frame(Path(args.vod), ts)
            l = _edge_density(img, left_box)
            r = _edge_density(img, right_box)
            diff = r - l
            print(f"  t={ts:7.1f}s  left={l:.4f}  right={r:.4f}  right-left={diff:+.4f}")
            lefts.append(l); rights.append(r)
        return lefts, rights

    b_lefts, b_rights = measure_both("SCENE B (just chatting)", args.scene_b)

    if args.scene_c:
        c_lefts, c_rights = measure_both("SCENE C (watch party)", args.scene_c)

    if args.scene_a:
        a_lefts, a_rights = measure_both("SCENE A (gaming)", args.scene_a)

    print("\n── Suggested thresholds ──")

    # scene_b_right_lead: right-left margin that cleanly separates scene_b
    b_diffs = [r - l for r, l in zip(b_rights, b_lefts)]
    print(f"\nscene_b (just chatting) right-left diffs: min={min(b_diffs):+.4f}  max={max(b_diffs):+.4f}")
    if args.scene_c:
        c_diffs = [r - l for r, l in zip(c_rights, c_lefts)]
        print(f"scene_c (watch party)  right-left diffs: min={min(c_diffs):+.4f}  max={max(c_diffs):+.4f}")
        if min(b_diffs) > max(c_diffs):
            lead = (min(b_diffs) + max(c_diffs)) / 2
            print(f"  → scene_b_right_lead: {lead:.4f}")
        else:
            print(f"  !! Overlap in right-lead — min B={min(b_diffs):+.4f}, max C={max(c_diffs):+.4f}")

        print(f"\nscene_c left densities: min={min(c_lefts):.4f}  max={max(c_lefts):.4f}")
        print(f"scene_b left densities: min={min(b_lefts):.4f}  max={max(b_lefts):.4f}")
        if min(c_lefts) > max(b_lefts):
            lmin = (min(c_lefts) + max(b_lefts)) / 2
            print(f"  → scene_c_left_min: {lmin:.4f}")
        else:
            # pick midpoint between max(b_lefts) and min(c_lefts) if they overlap slightly
            lmin = max(b_lefts) + 0.003
            print(f"  !! Some overlap — suggested scene_c_left_min: {lmin:.4f}  (tune manually)")

    if args.scene_a and args.scene_c:
        print(f"\nscene_a right densities: min={min(a_rights):.4f}  max={max(a_rights):.4f}")
        print(f"scene_c right densities: min={min(c_rights):.4f}  max={max(c_rights):.4f}")
        if min(a_rights) > max(c_rights):
            rmin = (min(a_rights) + max(c_rights)) / 2
            print(f"  → scene_a_right_min: {rmin:.4f}")
        else:
            print(f"  !! Overlap in right density A/C")


if __name__ == "__main__":
    main()
