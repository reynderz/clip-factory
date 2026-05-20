"""Render a clip in one ffmpeg pass.

Inputs are assigned dynamically based on what's needed:
  [0:v/a]  the VOD (always)
  next     the brainrot filler video (if layout == 'brainrot')
  next     the music track (if music_on)
  next     the chat overlay PNG (if chat_overlay_on)

Both video and audio go through filter_complex so we can sidechain
compression for music ducking without extra render passes.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from . import crop as crop_mod
from . import audio as audio_mod


def render_raw_clip(vod: Path, start: float, end: float, out_path: Path) -> Path:
    """Stream-copy cut from the source VOD — no crop, no encode, no overlays."""
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(vod),
        "-t", f"{end - start:.3f}",
        "-c", "copy",
        str(out_path),
    ], check=True)
    return out_path


def render_clip(vod: Path, start: float, end: float,
                layout: str, ass_path: Path,
                out_path: Path, cfg: dict,
                scene: str = "scene_a",
                chat_overlay_png: Path | None = None,
                chat_overlay_start: float = 0.0,
                chat_overlay_duration: float = 5.0,
                filler_path: Path | None = None,
                filler_start: float = 0.0,
                filler_loop: bool = False,
                music_on: bool = False,
                music_seed: int | None = None,
                project_root: Path | None = None,
                crop_override: dict | None = None,
                face_override: dict | None = None,
                subs_override: dict | None = None,
                swap: bool = False,
                game_override: dict | None = None,
                game_override2: dict | None = None,
                discord_sound: bool = False,
                censor_words: list | None = None) -> Path:
    ow, oh = cfg["output"]["resolution"]
    fps = cfg["output"]["fps"]
    crf = cfg["output"]["crf"]
    duration = end - start

    # ── Assemble ffmpeg inputs and track which slot is which ──
    inputs: list[str] = ["-ss", f"{start:.3f}", "-i", str(vod)]
    next_slot = 1   # [0:v] = VOD, next = 1

    filler_slot = None
    if layout in ("brainrot", "screen_filler"):
        if filler_path is None:
            raise ValueError(f"{layout} layout requires filler_path")
        filler_args = ["-stream_loop", "-1"] if filler_loop else []
        inputs += [*filler_args,
                   "-ss", f"{filler_start:.3f}", "-i", str(filler_path)]
        filler_slot = next_slot
        next_slot += 1

    # Audio plan: builds the audio filter chain and the music input args.
    audio_plan = audio_mod.plan_audio(cfg, music_on=music_on,
                                       music_seed=music_seed,
                                       project_root=project_root)
    music_slot = None
    if audio_plan.extra_inputs:
        # The audio module hardcodes [1:a] as the music label, which is
        # wrong if there's a filler video at slot 1. Patch the chain to
        # use whatever slot music actually lands in.
        music_slot = next_slot
        next_slot += 1
        inputs += audio_plan.extra_inputs
        if music_slot != 1:
            audio_plan.filter_chain = audio_plan.filter_chain.replace(
                "[1:a]", f"[{music_slot}:a]")

    chat_slot = None
    if chat_overlay_png is not None:
        inputs += ["-loop", "1", "-i", str(chat_overlay_png)]
        chat_slot = next_slot
        next_slot += 1

    ping_slot = None
    if discord_sound:
        inputs += ["-f", "lavfi", "-i",
                   "sine=frequency=880:sample_rate=44100:duration=0.25"]
        ping_slot = next_slot
        next_slot += 1

    # One discord notification input per curse word, delayed to its timestamp
    censor_slots: list[int] = []
    _discord_sfx = (project_root / "music" / "intro_sfx.mp3") if project_root else None
    if censor_words and _discord_sfx and _discord_sfx.exists():
        for _ in censor_words:
            inputs += ["-i", str(_discord_sfx)]
            censor_slots.append(next_slot)
            next_slot += 1

    intro_slot = None
    intro_sfx_cfg = cfg.get("intro_sound", {})
    if intro_sfx_cfg.get("enabled", False):
        sfx_path = intro_sfx_cfg.get("path")
        if sfx_path:
            sfx_file = Path(sfx_path)
            if not sfx_file.is_absolute() and project_root is not None:
                sfx_file = project_root / sfx_file
            if sfx_file.exists():
                inputs += ["-i", str(sfx_file)]
                intro_slot = next_slot
                next_slot += 1

    # ── Build the video filter chain ──
    layout_filter = crop_mod.build_filter(cfg, layout, scene,
                                          crop_override=crop_override,
                                          face_override=face_override,
                                          game_override=game_override,
                                          game_override2=game_override2,
                                          swap=swap)
    # The brainrot filter uses [1:v]. If filler_slot != 1, rewrite.
    if filler_slot is not None and filler_slot != 1:
        layout_filter = layout_filter.replace("[1:v]", f"[{filler_slot}:v]")

    stages = [layout_filter]
    current = "[v]"

    if chat_slot is not None:
        cc = cfg.get("chat_overlay", {})
        pos_y_frac = cc.get("position_y_frac", 0.50)
        fade = cc.get("fade_sec", 0.25)
        t0 = chat_overlay_start
        t1 = chat_overlay_start + chat_overlay_duration
        stages.append(
            f"[{chat_slot}:v]format=rgba,"
            f"fade=t=in:st={t0}:d={fade}:alpha=1,"
            f"fade=t=out:st={max(t0, t1 - fade):.2f}:d={fade}:alpha=1[chat]"
        )
        stages.append(
            f"{current}[chat]overlay=x=(W-w)/2:y=H*{pos_y_frac}-h/2:"
            f"enable='between(t,{t0},{t1})'[vo]"
        )
        current = "[vo]"

    # ffmpeg 8.x requires explicit filename= for the ass filter
    esc = str(ass_path).replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
    stages.append(f"{current}ass=filename={esc}[vout]")

    # ── Combine video and audio filter chains ──
    full_filter = ";".join(stages) + ";" + audio_plan.filter_chain
    final_audio = audio_plan.output_label
    if ping_slot is not None:
        full_filter += (
            f";[{ping_slot}:a]afade=t=in:st=0:d=0.02,"
            f"afade=t=out:st=0.15:d=0.08,volume=0.9[_ping]"
            f";{final_audio}[_ping]amix=inputs=2:duration=first[_aout_ping]"
        )
        final_audio = "[_aout_ping]"

    if intro_slot is not None:
        vol_db = intro_sfx_cfg.get("volume_db", 0.0)
        full_filter += (
            f";[{intro_slot}:a]volume={vol_db}dB[_intro_sfx]"
            f";{final_audio}[_intro_sfx]amix=inputs=2:duration=first:"
            f"dropout_transition=0[_aout_intro]"
        )
        final_audio = "[_aout_intro]"

    if censor_words:
        # Re-base audio PTS to 0 so between(t,...) matches clip-relative word timestamps.
        # Without this, t equals the original VOD timestamp (e.g. 2710 s) while
        # word timestamps are 0-based, so the mute expression never fires.
        full_filter += f";{final_audio}asetpts=PTS-STARTPTS[_anorm]"
        mute_expr = "+".join(
            f"between(t,{w['start']:.3f},{w['end']:.3f})" for w in censor_words
        )
        full_filter += f";[_anorm]volume=enable='{mute_expr}':volume=0[_cmuted]"
        final_audio = "[_cmuted]"

        if censor_slots:
            beep_labels = []
            for i, (slot, word) in enumerate(zip(censor_slots, censor_words)):
                delay_ms = int(word["start"] * 1000)
                full_filter += f";[{slot}:a]adelay={delay_ms}|{delay_ms}[_beep{i}]"
                beep_labels.append(f"[_beep{i}]")
            n = 1 + len(beep_labels)
            full_filter += (
                f";[_cmuted]{''.join(beep_labels)}"
                f"amix=inputs={n}:duration=first:normalize=0:dropout_transition=0[_aout_censor]"
            )
            final_audio = "[_aout_censor]"

    # ── Encoder ──
    encoder = cfg["output"].get("encoder", "libx264")
    video_enc_args = ["-c:v", encoder]
    if encoder == "libx264":
        video_enc_args += ["-preset", "medium", "-crf", str(crf)]
    elif encoder == "h264_videotoolbox":
        video_enc_args += ["-b:v",
                           cfg["output"].get("videotoolbox_bitrate", "8M")]
    else:
        video_enc_args += ["-crf", str(crf)]

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-t", f"{duration:.3f}",
        "-filter_complex", full_filter,
        "-map", "[vout]",
        "-map", final_audio,
        "-r", str(fps),
        *video_enc_args,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path
