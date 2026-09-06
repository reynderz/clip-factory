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

    python pipeline.py import_clips <vod_id> --dir /path/to/premade_clips
        Import a folder of already-cut clips as candidates instead of a VOD.
        Skips download + moment detection; each file becomes one candidate.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import random
import subprocess
import sys
from pathlib import Path

from stages import (fetch, detect_moments, detect_chat_reading,
                    transcribe_vod, classify_scene, transcribe, subtitle,
                    render, chat_overlay, censor, import_folder)
from stages.subtitle import apply_cuts_to_words
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


def _resolve_stinger(cfg: dict, project_root: Path) -> Path | None:
    """Return the stinger video path if configured, enabled, and exists."""
    sc = cfg.get("stinger", {})
    if not sc.get("enabled", True):
        return None
    p = sc.get("path", "")
    if not p:
        return None
    sp = Path(p)
    if not sp.is_absolute():
        sp = project_root / sp
    return sp if sp.exists() else None


def _render_one_variant(vod: Path, clip: dict, scene: str, words,
                        work: Path, cfg: dict, variant: dict,
                        out_dir: Path, idx: int,
                        force_chat: bool = False,
                        msgs: list | None = None,
                        subs_cli: dict | None = None,
                        music_file: str | None = None,
                        music_run_key: int = 0) -> Path | None:
    """Render a single variant for a single approved clip."""
    name = variant["name"]
    layout = variant["layout"]
    chat_on = force_chat or bool(variant.get("chat_overlay")) or bool(clip.get("chat_message"))
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

    # Subs priority: global < per-clip < variant < render-modal (subs_cli)
    clip_cfg = dict(cfg)
    if clip.get("subs"):
        clip_cfg["subs"] = {**cfg["subs"], **clip["subs"]}
    if "subs_y_frac" in variant:
        clip_cfg["subs"] = {**clip_cfg["subs"],
                            "position_y_frac": float(variant["subs_y_frac"])}
    if subs_cli:
        clip_cfg["subs"] = {**clip_cfg["subs"], **subs_cli}

    ass_path = work / f"clip_{idx:03d}__{name}.ass"

    # Chat overlay PNG, if this variant wants one
    overlay_png = None
    overlay_start = 0.0
    overlay_dur = cfg.get("chat_overlay", {}).get("visible_sec", 4.5)
    chat_overlays_list = None

    msgs_to_show = clip.get("chat_messages") or (
        [clip["chat_message"]] if clip.get("chat_message") else []
    )
    if chat_on and msgs_to_show:
        # Selected chat message(s) always open the clip — not synced to their
        # real chat timestamp or the clip's peak. The 1st message shows at
        # t=0; later ones stack back-to-back after it, unless the GUI set an
        # explicit display_start_sec on that message.
        stack_gap = cfg.get("chat_overlay", {}).get("stack_gap_sec", 0.3)
        if len(msgs_to_show) == 1:
            msg = msgs_to_show[0]
            overlay_png = work / f"clip_{idx:03d}__{name}_chat.png"
            chat_overlay.render_chat_overlay(
                username=msg["user"], message=msg["text"],
                out_path=overlay_png,
                frame_w=cfg["output"]["resolution"][0],
                font_name=cfg["subs"]["font"],
                font_size=cfg.get("chat_overlay", {}).get("font_size", 44),
            )
            overlay_start = 0.0
        else:
            chat_overlays_list = []
            prev_start = 0.0
            for mi, msg in enumerate(msgs_to_show):
                png = work / f"clip_{idx:03d}__{name}_chat_{mi}.png"
                chat_overlay.render_chat_overlay(
                    username=msg["user"], message=msg["text"],
                    out_path=png,
                    frame_w=cfg["output"]["resolution"][0],
                    font_name=cfg["subs"]["font"],
                    font_size=cfg.get("chat_overlay", {}).get("font_size", 44),
                )
                if mi == 0:
                    t = 0.0
                else:
                    override = msg.get("display_start_sec")
                    t = float(override) if override is not None else prev_start + overlay_dur + stack_gap
                chat_overlays_list.append((png, t, overlay_dur))
                prev_start = t

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

    # Music seed: high bits = random per-render-run key so the song order
    # is different every time. Low 12 bits = clip position so clips cycle
    # through all songs before repeating within one render session.
    if music_on:
        music_seed = (music_run_key << 12) | ((idx - 1) & 0xFFF)
    else:
        music_seed = None
    # Explicit song override from the render modal (solo render only)
    _music_file_path = None
    if music_on and music_file:
        p = Path(music_file)
        if not p.is_absolute():
            music_cfg = cfg.get("audio", {}).get("music", {})
            folder = Path(music_cfg.get("folder", "music"))
            if not folder.is_absolute():
                folder = ROOT / folder
            p = folder / music_file
        if p.exists():
            _music_file_path = p
            music_seed = None  # disable random shuffle when explicit file given

    # Use raw word dicts for censoring so the GUI's 'censored' flag is respected.
    # The Word dataclass drops custom fields; the raw dicts from clip["words"] keep them.
    _words_for_censor = clip.get("words") or words
    curse_hits = censor.find_curse_words(_words_for_censor, cfg)

    # Curse-word timestamps are in the ORIGINAL (pre-cut) clip timeline, but by
    # the time the mute filter runs, jump cuts have already trimmed/concatenated
    # the audio onto a shorter, shifted timeline. Without remapping, a hit whose
    # end lands past the new (shorter) duration mutes everything up to the
    # actual end of the clip instead of just the word — remap it the same way
    # subtitle words are (see words_for_ass below).
    clip_cuts = clip.get("cuts") or []
    duration_sec = end - start
    if clip_cuts and curse_hits:
        _hit_words = [transcribe.Word(text="", start=h["start"], end=h["end"])
                      for h in curse_hits]
        _hit_words = apply_cuts_to_words(_hit_words, clip_cuts, duration_sec)
        curse_hits = [{"start": w.start, "end": w.end} for w in _hit_words]

    # SFX triggers: words tagged with an sfx file in the GUI.
    # Like curse-word timestamps above, these are in the ORIGINAL (pre-cut)
    # clip timeline and must be remapped onto the post-cut timeline, or every
    # sfx after a jump cut plays increasingly out of sync with what it was
    # tagged to.
    _raw_sfx_triggers = [
        {"file": w["sfx"], "start": w["start"], "end": w["end"],
         "volume": w.get("sfx_volume", 0.5)}
        for w in (clip.get("words") or [])
        if w.get("sfx")
    ]
    if clip_cuts and _raw_sfx_triggers:
        _sfx_words = [transcribe.Word(text=str(i), start=t["start"], end=t["end"])
                      for i, t in enumerate(_raw_sfx_triggers)]
        _sfx_words = apply_cuts_to_words(_sfx_words, clip_cuts, duration_sec)
        _sfx_triggers = [
            {**_raw_sfx_triggers[int(w.text)], "start": w.start, "end": w.end}
            for w in _sfx_words
        ] or None
    else:
        _sfx_triggers = _raw_sfx_triggers or None
    if curse_hits:
        print(f"  censoring {len(curse_hits)} word(s)")

    # Title overlay PNG (Pillow-rendered so emoji work)
    title_png = None
    if clip.get("title"):
        from stages import title_overlay as _title_mod
        title_png = work / f"clip_{idx:03d}__{name}_title.png"
        _title_mod.render_title_png(clip["title"], clip_cfg, title_png)

    # Emoji reaction overlay
    from stages import emoji_overlay as _emoji_mod
    emoji_png = None
    emoji_start_t = 0.0
    emoji_dur = 2.5
    ec = cfg.get("emoji_overlay", {})
    if ec.get("enabled", False):
        reasons = clip.get("reasons", [])
        picked = _emoji_mod.pick_emojis(
            msgs or [], clip["peak_sec"], reasons,
            window_before=float(ec.get("window_before_sec", 15.0)),
            window_after=float(ec.get("window_after_sec", 5.0)),
            max_emojis=int(ec.get("max_emojis", 3)),
        )
        emoji_png = work / f"clip_{idx:03d}__{name}_emoji.png"
        _emoji_mod.render_emoji_png(picked, int(ec.get("font_size", 120)), emoji_png)
        peak_t_in_clip = clip["peak_sec"] - start
        emoji_start_t = max(0.0, peak_t_in_clip - float(ec.get("appear_before_sec", 0.3)))
        emoji_dur = float(ec.get("display_sec", 2.5))

    # Subtitle words adjusted for any jump cuts
    words_for_ass = words
    if clip_cuts:
        words_for_ass = apply_cuts_to_words(list(words), clip_cuts, duration_sec)
    if clip.get("subs_disabled"):
        words_for_ass = []

    # The render filter now uses a trim+setpts stage that normalises PTS to 0
    # at the exact clip start, so no seek-offset correction is needed.
    # Only apply an explicit per-clip delay if the user configured one.
    _extra_delay = float(clip_cfg.get("subs", {}).get("subtitle_delay_sec", 0.0))
    _sub_delay = round(_extra_delay, 3)
    subtitle.write_ass(words_for_ass, clip_cfg,
                       tuple(clip_cfg["output"]["resolution"]), ass_path,
                       delay_sec=_sub_delay)

    # Subtitle emoji theme: random emoji popping above the caption line,
    # roughly every N spoken words (independent of the peak reaction emoji above).
    from stages import subs_emoji_theme as _subs_emoji_mod
    subs_emoji_overlays = _subs_emoji_mod.build_emoji_theme_overlays(
        words_for_ass, clip_cfg, work, idx, name, delay_sec=_sub_delay,
    )

    out_path = out_dir / f"clip_{idx:03d}__{name}.mp4"
    _stinger_path = _resolve_stinger(cfg, ROOT) if clip.get("stinger") else None
    render.render_clip(
        vod, start, end, layout, ass_path, out_path, clip_cfg,
        scene=scene,
        chat_overlay_png=overlay_png,
        chat_overlay_start=overlay_start,
        chat_overlay_duration=overlay_dur,
        chat_overlays=chat_overlays_list,
        filler_path=filler_path,
        filler_start=filler_start,
        filler_loop=filler_loop,
        music_on=music_on,
        music_seed=music_seed,
        music_file=_music_file_path,
        project_root=ROOT,
        crop_override=_resolve_crop_override(clip),
        face_override=clip.get("face_center"),
        swap=bool(variant.get("swap", False)),
        game_override=clip.get("game_crop"),
        game_override2=clip.get("game_crop2"),
        discord_sound=bool(clip.get("discord_sound", False)),
        censor_words=curse_hits or None,
        peak_sec=clip["peak_sec"],
        emoji_png=emoji_png,
        emoji_start=emoji_start_t,
        emoji_duration=emoji_dur,
        subs_emoji_overlays=subs_emoji_overlays or None,
        cuts=clip_cuts or None,
        sfx_triggers=_sfx_triggers,
        title_png=title_png,
        stinger_path=_stinger_path,
    )
    return out_path


def cmd_render(args):
    cfg = load_global()
    vod_id = args.vod_id
    work = WORK / vod_id
    vod = work / f"{vod_id}.mp4"
    # Pair each clip with its position in the full approved list *before*
    # any --only-clip filtering, so filenames (clip_{idx:03d}__...) always
    # reflect the clip's real slot — otherwise a filtered run always starts
    # counting from 1 and silently overwrites whatever clip actually sits
    # at position 1's output files.
    approved = list(enumerate(json.loads((work / "approved.json").read_text()), 1))

    if args.only_clip is not None:
        approved = [(i, c) for i, c in approved
                    if abs(c["peak_sec"] - args.only_clip) < 0.01]

    # Render-modal subs overrides — applied LAST so they beat per-clip subs.
    # Position is also written directly into cfg["subs"] so that clips without
    # any saved subs (clip.get("subs") is falsy) still pick it up.
    subs_cli: dict = {}
    if getattr(args, "subs_y_frac", None) is not None:
        cfg["subs"]["position_y_frac"] = args.subs_y_frac   # clips without subs
        subs_cli["position_y_frac"]    = args.subs_y_frac   # clips with subs (post-merge)
    if getattr(args, "subs_font", None):
        subs_cli["font"] = args.subs_font
    if getattr(args, "subs_font_size", None) is not None:
        subs_cli["font_size"] = args.subs_font_size
    if getattr(args, "subs_style", None):
        cfg["subs"]["style"] = args.subs_style     # clips without subs
        subs_cli["style"] = args.subs_style        # clips with subs (post-merge)
    if getattr(args, "emoji_theme", False):
        cfg.setdefault("subs_emoji_theme", {})["enabled"] = True

    force_chat = getattr(args, "force_chat", False)
    # Applies whenever ANY clip shows chat — not just when force_chat is on,
    # since a clip with a per-clip selected message shows chat regardless.
    if getattr(args, "chat_y_frac", None) is not None:
        cfg["chat_overlay"]["position_y_frac"] = args.chat_y_frac

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

    # Load chat messages for emoji overlay (cached on disk from detect phase)
    msgs: list = []
    chat_json = work / f"{vod_id}_chat.json"
    if chat_json.exists():
        try:
            msgs = fetch.download_chat(vod_id, work)
        except Exception:
            pass

    print(f"Rendering {len(approved)} approved clips through "
          f"{len(all_variants)} variants (max).")

    # Random per-run key — ensures song selection differs each render session
    # while still cycling through all songs without repeats within the session.
    music_run_key = random.randint(0, (1 << 20) - 1)

    total_rendered = 0
    for i, c in approved:
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

        # Word priority:
        #  1. GUI-edited / GUI-transcribed words saved in approved.json  (most trusted)
        #  2. Full-VOD coarse transcript (fast, no re-encode, good accuracy)
        #  3. Fresh per-clip whisper transcription (slowest, last resort)
        if c.get("words"):
            words = [transcribe.Word(text=w["text"],
                                     start=float(w["start"]),
                                     end=float(w["end"]))
                     for w in c["words"]]
            print(f"  using {len(words)} GUI words")
        else:
            vod_transcript_path = work / "vod_transcript.json"
            vod_words: list[transcribe.Word] = []
            if vod_transcript_path.exists():
                raw = json.loads(vod_transcript_path.read_text())
                for w in raw:
                    ws, we = float(w["start"]), float(w["end"])
                    if ws >= start and we <= end + 0.5:
                        vod_words.append(transcribe.Word(
                            text=w["text"],
                            start=round(ws - start, 3),
                            end=round(we - start, 3),
                        ))
            if vod_words:
                words = vod_words
                print(f"  using {len(words)} words from vod_transcript")
            else:
                clip_audio = work / f"clip_{i:03d}.wav"
                _pts_off = render.vod_pts_offset(vod)
                _seek_pos = start + _pts_off
                _margin   = 10.0
                _pre      = max(0.0, _seek_pos - _margin)
                _fine     = _seek_pos - _pre
                subprocess.run([
                    "ffmpeg", "-y",
                    "-fflags", "+genpts",
                    "-ss", f"{_pre:.3f}", "-i", str(vod),
                    "-ss", f"{_fine:.3f}",
                    "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                    str(clip_audio),
                ], check=True, capture_output=True, timeout=render._FFMPEG_TIMEOUT)
                words = transcribe.transcribe(clip_audio)
                print(f"  transcribed {len(words)} words (fresh whisper)")

        for v in applicable:
            try:
                out = _render_one_variant(
                    vod, c, scene, words, work, cfg, v, out_root, i,
                    force_chat=force_chat, msgs=msgs,
                    subs_cli=subs_cli,
                    music_file=getattr(args, "music_file", None),
                    music_run_key=music_run_key)
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
    max_sec = args.max_sec if args.max_sec and args.max_sec < duration else duration
    if max_sec < duration:
        print(f"  VOD: {vod.name}  {duration:.0f}s → limiting to first {max_sec:.0f}s, chat msgs: {len(msgs)}")
        msgs = [m for m in msgs if m.offset_sec <= max_sec]
    else:
        print(f"  VOD: {vod.name}  {duration:.0f}s, chat msgs: {len(msgs)}")

    print("[2/3] Detecting hype moments…")
    cands = detect_moments.detect(vod, msgs, max_sec, cfg)
    cands = [c for c in cands if c.start_sec < max_sec]
    print(f"  {len(cands)} hype candidates")

    if cfg.get("chat_reading", {}).get("enabled", True):
        print(f"[3/3] Transcribing first {max_sec:.0f}s for chat-reading detection (~15-25 min first run)…")
        tpath = work / "vod_transcript.json"
        transcribe_vod.transcribe_full_vod(vod, tpath, max_sec=max_sec)
        chat_events = detect_chat_reading.detect_chat_reads(tpath, msgs, max_sec, cfg)
        detect_chat_reading.save_events(chat_events, work / "chat_reads.json")
        print(f"  {len(chat_events)} chat-reading moments")
        from stages.detect_moments import Candidate
        lead = cfg["detection"]["clip_lead_sec"]
        trail = cfg["detection"]["clip_trail_sec"]
        extra = [Candidate(
            start_sec=max(0.0, e.peak_sec - lead),
            end_sec=min(max_sec, e.peak_sec + trail),
            peak_sec=e.peak_sec,
            score=1.5 + (e.score - 55) / 100,
            reasons=[f"chat_read={e.score:.0f}"],
        ) for e in chat_events]
        cands = detect_moments._merge(cands + extra, cfg["detection"]["min_gap_sec"])
        cands = [c for c in cands if c.start_sec < max_sec]
        cands.sort(key=lambda c: -c.score)
        cands = cands[:cfg["detection"]["max_candidates_per_vod"]]
    else:
        print("[3/3] Chat-reading detection disabled.")

    (work / "candidates.json").write_text(
        json.dumps([dataclasses.asdict(c) for c in cands], indent=2))
    print(f"Done. {len(cands)} candidates saved to {work / 'candidates.json'}")
    print(f"Open the GUI: python gui.py {vod_id}")


# ─────────────────────────── import_clips ───────────────────────
def cmd_import_clips(args):
    """Import a folder of premade/pre-cut clips as candidates, skipping
    VOD download and hype-moment detection entirely."""
    vod_id = args.vod_id
    work = WORK / vod_id; work.mkdir(parents=True, exist_ok=True)
    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"Not a folder: {folder}")

    print(f"[1/2] Scanning {folder} for clip files…")
    out_vod = work / f"{vod_id}.mp4"
    print("[2/2] Normalising + concatenating clips into a single project VOD…")
    cands = import_folder.import_folder(folder, out_vod)

    (work / "candidates.json").write_text(json.dumps(cands, indent=2))
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
    p2.add_argument("--subs-font", type=str, default=None,
                    help="Override subtitle font name")
    p2.add_argument("--subs-font-size", type=int, default=None,
                    help="Override subtitle font size")
    p2.add_argument("--subs-style", type=str, default=None,
                    choices=["karaoke", "word_pop", "none"],
                    help="karaoke = full line, active word highlighted (default); "
                         "word_pop = one word at a time, MrBeast-style bounce; "
                         "none = no subtitles")
    p2.add_argument("--emoji-theme", action="store_true", default=False,
                    help="Pop random emoji above the subtitles as words are spoken")
    p2.add_argument("--force-chat", action="store_true", default=False,
                    help="Force chat overlay on for all variants (only clips with a message selected)")
    p2.add_argument("--chat-y-frac", type=float, default=None,
                    help="Override chat overlay vertical position (0=top, 1=bottom)")
    p2.add_argument("--music-file", type=str, default=None,
                    help="Use this specific music file (filename only, relative to music/ folder)")
    p2.set_defaults(func=cmd_render)

    p3 = sub.add_parser("auto"); p3.add_argument("vod_id")
    p3.add_argument("--top", type=int, default=10)
    p3.add_argument("--only-variants", default="")
    p3.set_defaults(func=cmd_auto)

    p4 = sub.add_parser("variants")
    p4.set_defaults(func=cmd_variants)

    p5 = sub.add_parser("detect_only"); p5.add_argument("vod_id")
    p5.add_argument("--max-sec", type=float, default=None,
                    help="Only detect candidates up to this many seconds into the VOD")
    p5.set_defaults(func=cmd_detect_only)

    p6 = sub.add_parser("gui"); p6.add_argument("vod_id")
    p6.set_defaults(func=cmd_gui)

    p7 = sub.add_parser("import_clips")
    p7.add_argument("vod_id", help="Project name to create/overwrite")
    p7.add_argument("--dir", required=True,
                    help="Folder of premade clip video files to import")
    p7.set_defaults(func=cmd_import_clips)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
