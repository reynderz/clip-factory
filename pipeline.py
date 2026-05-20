"""Clip factory orchestrator (variant-matrix model).

Usage:
    python pipeline.py detect <vod_id>
        Fetch VOD+chat, transcribe, detect moments, review interactively.

    python pipeline.py render <vod_id>
        Render every approved clip through every applicable variant.
        Outputs land in out/<vod_id>/<clip_NNN>__<variant>.mp4.

    python pipeline.py render <vod_id> --only-variants centered_chat_music,parkour_chat_music
        Render only the named variants (comma-separated).

    python pipeline.py variants
        List configured variants and which scenes they apply to.

    python pipeline.py auto <vod_id> --top 10
        Skip review, render top-N candidates through all variants.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

from stages import (fetch, detect_moments, detect_chat_reading,
                    transcribe_vod, classify_scene, transcribe, subtitle,
                    render, chat_overlay, censor)
from stages.cfg import load_global, load_variants, variant_applies
from review import review as review_cli


ROOT = Path(__file__).parent
WORK = ROOT / "work"

# Injected for clips where layout_mode == "include_gameplay"
_SPLIT_VARIANTS = [
    {"name": "split_chat_nomusic",   "layout": "split", "chat_overlay": True,  "music": False, "apply_to": "scene_a_only"},
    {"name": "split_nochat_nomusic", "layout": "split", "chat_overlay": False, "music": False, "apply_to": "scene_a_only"},
]



def get_duration(vod: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(vod)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


# ─────────────────────────── detect ────────────────────────────
def cmd_detect(args):
    cfg = load_global()
    vod_id = args.vod_id
    work = WORK / vod_id; work.mkdir(parents=True, exist_ok=True)

    print("[1/4] Fetching VOD + chat…")
    vod = fetch.download_vod(vod_id, work)
    msgs = fetch.download_chat(vod_id, work)
    duration = get_duration(vod)
    print(f"  VOD: {vod.name}  {duration:.0f}s, chat msgs: {len(msgs)}")

    print("[2/4] Detecting hype moments…")
    cands = detect_moments.detect(vod, msgs, duration, cfg)
    print(f"  {len(cands)} hype candidates")

    if cfg.get("chat_reading", {}).get("enabled", True):
        print("[3/4] Transcribing VOD for chat-reading detection…")
        print("       (~15-25 min on first run; cached after)")
        tpath = work / "vod_transcript.json"
        transcribe_vod.transcribe_full_vod(vod, tpath)

        chat_events = detect_chat_reading.detect_chat_reads(
            tpath, msgs, duration, cfg)
        detect_chat_reading.save_events(chat_events, work / "chat_reads.json")
        print(f"  {len(chat_events)} chat-reading moments")

        from stages.detect_moments import Candidate
        lead = cfg["detection"]["clip_lead_sec"]
        trail = cfg["detection"]["clip_trail_sec"]
        extra = [Candidate(
            start_sec=max(0.0, e.peak_sec - lead),
            end_sec=min(duration, e.peak_sec + trail),
            peak_sec=e.peak_sec,
            score=1.5 + (e.score - 55) / 100,
            reasons=[f"chat_read={e.score:.0f}"],
        ) for e in chat_events]
        cands = detect_moments._merge(cands + extra,
                                      cfg["detection"]["min_gap_sec"])
        cands.sort(key=lambda c: -c.score)
        cands = cands[:cfg["detection"]["max_candidates_per_vod"]]
    else:
        print("[3/4] Chat-reading detection disabled.")

    (work / "candidates.json").write_text(json.dumps(
        [dataclasses.asdict(c) for c in cands], indent=2))
    print(f"  total {len(cands)} candidates")

    print("[4/4] Review…")
    review_cli(
        candidates=[dataclasses.asdict(c) for c in cands],
        vod=vod,
        preview_dir=work / "previews",
        out_path=work / "approved.json",
        cfg=cfg,
        transcript_path=work / "vod_transcript.json",
        msgs=msgs,
    )


# ─────────────────────────── render ────────────────────────────
def _resolve_crop_override(clip: dict) -> dict | None:
    """Return the webcam crop override for a clip, or None.

    Priority: cam_crop (explicit webcam edit) > crop (generic drag box).
    The GUI always serialises crop as {x:0,y:0,w:1,h:1} when no box was
    drawn, so we only treat it as an override when it's non-trivial.
    """
    cam = clip.get("cam_crop")
    if cam:
        return cam
    raw = clip.get("crop")
    if raw is None:
        return None
    # Skip the full-frame default that the GUI emits when no box is drawn.
    if (raw.get("x", 0) == 0 and raw.get("y", 0) == 0
            and raw.get("w", 1) >= 0.999 and raw.get("h", 1) >= 0.999):
        return None
    return raw


def _render_one_variant(vod: Path, clip: dict, scene: str, words,
                        work: Path, cfg: dict, variant: dict,
                        out_dir: Path, idx: int) -> Path | None:
    """Render a single variant for a single approved clip."""
    name = variant["name"]
    layout = variant["layout"]
    chat_on = bool(variant.get("chat_overlay"))
    music_on = bool(variant.get("music"))

    start, end = clip["start_sec"], clip["end_sec"]

    if layout == "raw":
        out_path = out_dir / f"clip_{idx:03d}__raw.mp4"
        render.render_raw_clip(vod, start, end, out_path)
        return out_path

    if layout == "gameplay_fill":
        if not clip.get("game_crop"):
            print(f"    SKIP {name}: gameplay_fill requires the green crop box — edit clip and draw it")
            return None

    if layout == "crop_fill":
        if not _resolve_crop_override(clip) or not clip.get("game_crop"):
            print(f"    SKIP {name}: Fill requires both boxes — edit clip in Fill mode (purple=webcam, green=gameplay)")
            return None

    if layout == "dual_crop":
        if not clip.get("game_crop") or not clip.get("game_crop2"):
            print(f"    SKIP {name}: both crop boxes required — edit clip in Dual mode first")
            return None

    # Per-clip subtitle override, then per-variant position override
    clip_cfg = dict(cfg)
    if clip.get("subs"):
        clip_cfg["subs"] = {**cfg["subs"], **clip["subs"]}
    if "subs_y_frac" in variant:
        clip_cfg["subs"] = {**clip_cfg["subs"],
                            "position_y_frac": float(variant["subs_y_frac"])}

    # Subs (one global style, render once per variant since the .ass file
    # name needs to be unique per variant for parallel-safe runs)
    ass_path = work / f"clip_{idx:03d}__{name}.ass"
    subtitle.write_ass(words, clip_cfg,
                       tuple(clip_cfg["output"]["resolution"]), ass_path)

    # Chat overlay PNG, if this variant wants one
    overlay_png = None
    overlay_start = 0.0
    overlay_dur = cfg.get("chat_overlay", {}).get("visible_sec", 4.5)
    msg = clip.get("chat_message")
    if chat_on and msg:
        overlay_png = work / f"clip_{idx:03d}__{name}_chat.png"
        chat_overlay.render_chat_overlay(
            username=msg["user"], message=msg["text"],
            out_path=overlay_png,
            frame_w=cfg["output"]["resolution"][0],
            font_name=cfg["subs"]["font"],
            font_size=cfg.get("chat_overlay", {}).get("font_size", 44),
        )
        lead = cfg.get("chat_overlay", {}).get("lead_sec", 0.5)
        overlay_start = max(0.0, (clip["peak_sec"] - start) - lead)

    # Filler (brainrot only)
    filler_path = None
    filler_start = 0.0
    filler_loop = False
    if layout in ("brainrot", "screen_filler"):
        from stages.filler import pick_filler
        filler_dir = Path(variant.get("filler_dir", "fillers/parkour"))
        if not filler_dir.is_absolute():
            filler_dir = ROOT / filler_dir
        seed = idx * 1000 + sum(ord(c) for c in name) % 1000
        pick = pick_filler(filler_dir, end - start, seed=seed)
        if pick is None:
            print(f"    SKIP {name}: no filler in {filler_dir}")
            return None
        filler_path = pick.path
        filler_start = pick.start_sec
        filler_loop = pick.needs_loop

    # Music seed: stable per (clip, variant) so re-renders match.
    music_seed = idx * 10000 + sum(ord(c) for c in name) if music_on else None

    curse_hits = censor.find_curse_words(words, cfg)
    if curse_hits:
        print(f"  censoring {len(curse_hits)} word(s)")

    out_path = out_dir / f"clip_{idx:03d}__{name}.mp4"
    render.render_clip(
        vod, start, end, layout, ass_path, out_path, clip_cfg,
        scene=scene,
        chat_overlay_png=overlay_png,
        chat_overlay_start=overlay_start,
        chat_overlay_duration=overlay_dur,
        filler_path=filler_path,
        filler_start=filler_start,
        filler_loop=filler_loop,
        music_on=music_on,
        music_seed=music_seed,
        project_root=ROOT,
        crop_override=_resolve_crop_override(clip),
        face_override=clip.get("face_center"),
        swap=bool(variant.get("swap", False)),
        game_override=clip.get("game_crop"),
        game_override2=clip.get("game_crop2"),
        discord_sound=bool(clip.get("discord_sound", False)),
        censor_words=curse_hits or None,
    )
    return out_path


def cmd_render(args):
    cfg = load_global()
    vod_id = args.vod_id
    work = WORK / vod_id
    vod = work / f"{vod_id}.mp4"
    approved = json.loads((work / "approved.json").read_text())

    if args.only_clip is not None:
        approved = [c for c in approved if abs(c["peak_sec"] - args.only_clip) < 0.01]

    if getattr(args, "subs_y_frac", None) is not None:
        cfg["subs"]["position_y_frac"] = args.subs_y_frac

    _DUAL_LAYOUTS = {"dual_screen", "screen_cam_split", "screen_filler"}

    # All variants from config — used as the full catalogue for dual injection.
    all_cfg_variants = load_variants(cfg)

    all_variants = list(all_cfg_variants)
    if args.only_variants:
        wanted = {v.strip() for v in args.only_variants.split(",")}
        all_variants = [v for v in all_variants if v["name"] in wanted]
        if not all_variants:
            raise SystemExit(f"No variants match {wanted}")

    out_root = Path(cfg["output"]["dir"]) / vod_id
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"Rendering {len(approved)} approved clips through "
          f"{len(all_variants)} variants (max).")

    total_rendered = 0
    for i, c in enumerate(approved, 1):
        start, end = c["start_sec"], c["end_sec"]
        scene = classify_scene.classify_scene(vod, start, end, cfg)
        has_msg = c.get("chat_message") is not None

        # Dual-layout variants are never part of the normal pool — they're
        # only added explicitly when the clip has layout_mode == "dual_screen".
        applicable = [v for v in all_variants
                      if variant_applies(v, scene, has_msg)
                      and v.get("layout") not in _DUAL_LAYOUTS]

        # Per-clip "include gameplay" mode: inject split variants on top.
        if c.get("layout_mode") == "include_gameplay":
            for sv in _SPLIT_VARIANTS:
                if variant_applies(sv, scene, has_msg):
                    applicable.append(sv)

        # Per-clip "dual screen" mode: pull dual variants straight from the
        # full config (all_cfg_variants), bypassing any only_variants filter.
        # This guarantees they always render when the user set layout_mode.
        if c.get("layout_mode") == "dual_screen":
            existing = {v["name"] for v in applicable}
            for sv in all_cfg_variants:
                if sv.get("layout") in _DUAL_LAYOUTS and sv["name"] not in existing:
                    applicable.append(sv)


        print(f"\n[{i}/{len(approved)}] t={start:.1f}-{end:.1f}s  "
              f"scene={scene}  msg={'yes' if has_msg else 'no'}  "
              f"→ {len(applicable)} variants")

        if not applicable:
            continue

        # Use GUI-edited words if present, otherwise transcribe.
        if c.get("words"):
            words = [transcribe.Word(text=w["text"],
                                     start=float(w["start"]),
                                     end=float(w["end"]))
                     for w in c["words"]]
            print(f"  using {len(words)} edited words from GUI")
        else:
            clip_audio = work / f"clip_{i:03d}.wav"
            subprocess.run([
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(vod),
                "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                str(clip_audio),
            ], check=True, capture_output=True)
            words = transcribe.transcribe(clip_audio)
            print(f"  transcribed {len(words)} words")

        for v in applicable:
            try:
                out = _render_one_variant(
                    vod, c, scene, words, work, cfg, v, out_root, i)
                if out:
                    total_rendered += 1
                    print(f"    → {out.name}")
            except subprocess.CalledProcessError as e:
                print(f"    !! {v['name']}: ffmpeg failed ({e})")
            except Exception as e:
                print(f"    !! {v['name']}: {e}")

    print(f"\nDone. {total_rendered} files in {out_root}")


# ─────────────────────────── detect_only ───────────────────────
def cmd_detect_only(args):
    """Detect moments and save candidates.json but skip interactive review."""
    cfg = load_global()
    vod_id = args.vod_id
    work = WORK / vod_id; work.mkdir(parents=True, exist_ok=True)

    print("[1/3] Fetching VOD + chat…")
    vod = fetch.download_vod(vod_id, work)
    msgs = fetch.download_chat(vod_id, work)
    duration = get_duration(vod)
    print(f"  VOD: {vod.name}  {duration:.0f}s, chat msgs: {len(msgs)}")

    print("[2/3] Detecting hype moments…")
    cands = detect_moments.detect(vod, msgs, duration, cfg)
    print(f"  {len(cands)} hype candidates")

    if cfg.get("chat_reading", {}).get("enabled", True):
        print("[3/3] Transcribing VOD for chat-reading detection (~15-25 min first run)…")
        tpath = work / "vod_transcript.json"
        transcribe_vod.transcribe_full_vod(vod, tpath)
        chat_events = detect_chat_reading.detect_chat_reads(tpath, msgs, duration, cfg)
        detect_chat_reading.save_events(chat_events, work / "chat_reads.json")
        print(f"  {len(chat_events)} chat-reading moments")
        from stages.detect_moments import Candidate
        lead = cfg["detection"]["clip_lead_sec"]
        trail = cfg["detection"]["clip_trail_sec"]
        extra = [Candidate(
            start_sec=max(0.0, e.peak_sec - lead),
            end_sec=min(duration, e.peak_sec + trail),
            peak_sec=e.peak_sec,
            score=1.5 + (e.score - 55) / 100,
            reasons=[f"chat_read={e.score:.0f}"],
        ) for e in chat_events]
        cands = detect_moments._merge(cands + extra, cfg["detection"]["min_gap_sec"])
        cands.sort(key=lambda c: -c.score)
        cands = cands[:cfg["detection"]["max_candidates_per_vod"]]
    else:
        print("[3/3] Chat-reading detection disabled.")

    (work / "candidates.json").write_text(
        json.dumps([dataclasses.asdict(c) for c in cands], indent=2))
    print(f"Done. {len(cands)} candidates saved to {work / 'candidates.json'}")
    print(f"Open the GUI: python gui.py {vod_id}")


# ─────────────────────────── gui subcommand ─────────────────────
def cmd_gui(args):
    import subprocess as _sp
    _sp.run([sys.executable, str(ROOT / "gui.py"), args.vod_id])


# ─────────────────────────── auto / variants ───────────────────
def cmd_auto(args):
    cfg = load_global()
    vod_id = args.vod_id
    work = WORK / vod_id; work.mkdir(parents=True, exist_ok=True)

    vod = fetch.download_vod(vod_id, work)
    msgs = fetch.download_chat(vod_id, work)
    duration = get_duration(vod)
    cands = detect_moments.detect(vod, msgs, duration, cfg)[:args.top]
    (work / "approved.json").write_text(json.dumps(
        [dataclasses.asdict(c) for c in cands], indent=2))
    print(f"Auto-approved top {len(cands)}; rendering all variants…")
    cmd_render(args)


def cmd_variants(args):
    cfg = load_global()
    vs = load_variants(cfg)
    if not vs:
        print("No variants configured.")
        return
    print(f"{len(vs)} variants:")
    for v in vs:
        chat = "chat" if v.get("chat_overlay") else "no-chat"
        music = "music" if v.get("music") else "no-music"
        scope = v.get("apply_to", "both")
        layout = v["layout"]
        if layout == "brainrot":
            layout = f"brainrot ({v.get('filler_dir', '?')})"
        print(f"  {v['name']:30s}  {scope:14s}  {layout:30s}  {chat:8s} {music}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("detect"); p1.add_argument("vod_id")
    p1.set_defaults(func=cmd_detect)

    p2 = sub.add_parser("render"); p2.add_argument("vod_id")
    p2.add_argument("--only-variants", default="",
                    help="Comma-separated variant names; default = all")
    p2.add_argument("--only-clip", type=float, default=None,
                    help="Only render the clip with this peak_sec")
    p2.add_argument("--subs-y-frac", type=float, default=None,
                    help="Override subtitle vertical position (0=top, 1=bottom)")
    p2.set_defaults(func=cmd_render)

    p3 = sub.add_parser("auto"); p3.add_argument("vod_id")
    p3.add_argument("--top", type=int, default=10)
    p3.add_argument("--only-variants", default="")
    p3.set_defaults(func=cmd_auto)

    p4 = sub.add_parser("variants")
    p4.set_defaults(func=cmd_variants)

    p5 = sub.add_parser("detect_only"); p5.add_argument("vod_id")
    p5.set_defaults(func=cmd_detect_only)

    p6 = sub.add_parser("gui"); p6.add_argument("vod_id")
    p6.set_defaults(func=cmd_gui)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
