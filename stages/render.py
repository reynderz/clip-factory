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
import functools
import subprocess
import tempfile
from pathlib import Path

from . import crop as crop_mod
from . import audio as audio_mod
from . import subtitle as subtitle_mod

# ── Stinger: layouts where gameplay continues behind the stinger ───
# webcam on TOP, gameplay on BOTTOM — stinger goes in webcam slot
_STINGER_WEBCAM_TOP = {"split", "crop_fill"}
# gameplay on TOP, webcam on BOTTOM — stinger goes in cam slot
_STINGER_WEBCAM_BOT = {"screen_cam_split"}
# stinger acts as the filler video (bottom slot)
_STINGER_AS_FILLER  = {"brainrot", "screen_filler"}
# watch_party: stinger in cam (top 40%), VOD content bottom (60%)
_STINGER_WATCHPARTY = {"watch_party"}

_STINGER_SPLIT = (_STINGER_WEBCAM_TOP | _STINGER_WEBCAM_BOT |
                   _STINGER_AS_FILLER | _STINGER_WATCHPARTY)

# Twitch VODs occasionally carry non-monotonic DTS around ad-break
# discontinuities; feeding that straight into a filter_complex graph (esp.
# with sidechaincompress/amix) can deadlock ffmpeg at 0% CPU instead of
# erroring out. `_run_ffmpeg` bounds every render so a stall fails the clip
# instead of hanging the pipeline (and the GUI's render state) forever.
_FFMPEG_TIMEOUT = 300  # seconds


def _run_ffmpeg(cmd: list[str], timeout: float = _FFMPEG_TIMEOUT) -> None:
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg stalled for {timeout:.0f}s and was killed "
            f"(likely non-monotonic DTS in the source VOD): {' '.join(cmd[:8])}..."
        ) from e


@functools.lru_cache(maxsize=8)
def vod_pts_offset(vod: Path) -> float:
    """Return the container start_time (seconds).

    yt-dlp preserves the original HLS broadcast PTS when downloading Twitch
    VODs, so the file's first frame has PTS = start_time, not 0.  The browser
    player normalises this to 0, meaning browser time T maps to file PTS
    T + start_time.  Every ffmpeg -ss seek must add this offset.
    """
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=start_time",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(vod),
        ], capture_output=True, text=True, timeout=10)
        v = r.stdout.strip()
        if v and v != "N/A":
            return max(0.0, float(v))
    except Exception:
        pass
    return 0.0


def subtitle_seek_offset(vod: Path, start: float) -> float:
    """Return seconds to add to ASS timestamps to correct for keyframe-seek offset.

    With fast input seek (-ss before -i), ffmpeg's output PTS is normalised to 0
    at the first decoded packet (the I-frame / keyframe), NOT at the requested
    start position.  The ASS filter therefore maps ASS t=0 to the keyframe, making
    subtitles appear early by (start - keyframe_DTS) seconds.  This function
    measures that offset so callers can shift all subtitle timestamps forward.

    start is in browser time (0-based); the ffprobe read uses the file-PTS
    position (start + vod_pts_offset) so we probe the correct keyframe.
    """
    seek_pts = start + vod_pts_offset(vod)
    try:
        result = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "packet=dts_time,flags",
            "-read_intervals", f"{max(0, seek_pts - 10.0):.3f}%{seek_pts:.3f}",
            "-of", "csv=p=0",
            str(vod),
        ], capture_output=True, text=True, timeout=15)
        last_kf_dts = None
        for line in result.stdout.splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                dts = float(parts[0])
                if "K" in parts[1]:
                    last_kf_dts = dts
            except (ValueError, IndexError):
                pass
        if last_kf_dts is not None:
            return round(max(0.0, seek_pts - last_kf_dts), 3)
    except Exception:
        pass
    return 0.0


def _video_enc_args(cfg: dict, has_cuts: bool = False) -> list[str]:
    encoder = cfg["output"].get("encoder", "libx264")
    # h264_nvenc's -force_key_frames (used to plant a keyframe at each jump-cut
    # splice point) is unreliable in practice: confirmed across a real batch
    # that identical code/flags land the keyframe for some layouts/variants
    # and silently drop it for others, with no consistent pattern tied to
    # preset, rc-lookahead, bf, or forced-idr settings. Rather than risk a
    # cut re-predicting from unrelated content, cut clips fall back to
    # libx264, which reliably auto-detects the scene change itself
    # (scenecut=40) and always inserts a real I-frame there.
    if has_cuts and encoder == "h264_nvenc":
        encoder = "libx264"
    args = ["-c:v", encoder]
    if encoder == "libx264":
        args += ["-preset", "medium", "-crf", str(cfg["output"]["crf"])]
    elif encoder == "h264_nvenc":
        # cq mirrors crf (lower = higher quality); b:v 0 lets cq drive quality
        # instead of a fixed bitrate cap.
        args += ["-preset", "p5", "-rc", "vbr", "-cq", str(cfg["output"]["crf"]),
                  "-b:v", "0"]
    elif encoder == "h264_videotoolbox":
        args += ["-b:v", cfg["output"].get("videotoolbox_bitrate", "8M")]
    else:
        args += ["-crf", str(cfg["output"]["crf"])]
    # Some filter-graph combinations (notably the plain crop/scale chain used
    # by gameplay_zoom, which has no downstream overlay/blend step to
    # normalise format) let libavfilter auto-negotiate up to yuv444p. That
    # profile has little to no hardware decode support, so playback on phones
    # and lightweight players stutters/hiccups even though the encode itself
    # succeeds. Pin the delivery-standard 4:2:0 format explicitly so this
    # never depends on filter-graph guesswork.
    args += ["-pix_fmt", "yuv420p"]
    return args


def _render_stinger_segment(stinger_path: Path, layout: str,
                             cfg: dict, out_path: Path,
                             vod: Path | None = None,
                             vod_end: float = 0.0,
                             scene: str = "scene_a",
                             crop_override: dict | None = None,
                             face_override: dict | None = None,
                             game_override: dict | None = None,
                             game_override2: dict | None = None,
                             swap: bool = False,
                             project_root: Path | None = None) -> None:
    """Render the stinger segment.

    Split layouts: stinger in webcam slot, VOD gameplay continues from vod_end.
    All others: stinger scaled to full output resolution.
    The TikTok overlay PNG is applied on top in both cases.
    """
    ow, oh = cfg["output"]["resolution"]
    fps = cfg["output"]["fps"]
    sw, sh = cfg["layout"]["source_resolution"]

    overlay_path: Path | None = None
    if project_root is not None:
        _op = project_root / "overlay" / "renderoverlaytiktok.png"
        if _op.exists():
            overlay_path = _op

    stinger_dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(stinger_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip())

    def _gcrop() -> str:
        """Game crop expression for the current layout / overrides."""
        if game_override:
            return (f"iw*{game_override['w']}:ih*{game_override['h']}:"
                    f"iw*{game_override['x']}:ih*{game_override['y']}")
        _, gbox = crop_mod._scene_boxes(cfg, scene)
        if gbox:
            gx, gy, gw, gh = crop_mod._px(gbox, sw, sh)
            return f"{gw}:{gh}:{gx}:{gy}"
        return f"{sw}:{sh}:0:0"

    if layout in _STINGER_SPLIT and vod is not None:
        _pts_off = vod_pts_offset(vod)
        inputs = ["-fflags", "+genpts",
                  "-ss", f"{vod_end + _pts_off:.3f}", "-i", str(vod),  # slot 0
                  "-i", str(stinger_path)]                               # slot 1
        ol_slot = None
        if overlay_path:
            inputs += ["-loop", "1", "-i", str(overlay_path)]
            ol_slot = 2

        if layout in _STINGER_WEBCAM_TOP:
            cam_h  = int(oh * 0.45)
            game_h = oh - cam_h
            gc = _gcrop()
            filt = (
                f"[0:v]crop={gc},split=2[g_bg][g_fg];"
                f"[g_bg]scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{game_h},boxblur=30:2[g_bgb];"
                f"[g_fg]scale={ow}:{game_h}:force_original_aspect_ratio=decrease"
                f":force_divisible_by=2[g_fgs];"
                f"[g_bgb][g_fgs]overlay=(W-w)/2:(H-h)/2[game];"
                f"[1:v]scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{cam_h}[cam];"
                f"[cam][game]vstack=inputs=2[v]"
            )

        elif layout in _STINGER_WEBCAM_BOT:
            top_h = oh // 2
            bot_h = oh - top_h
            gc = _gcrop()
            filt = (
                f"[0:v]crop={gc},"
                f"scale={ow}:{top_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{top_h}[top];"
                f"[1:v]scale={ow}:{bot_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{bot_h}[bot];"
                f"[top][bot]vstack=inputs=2[v]"
            )

        elif layout in _STINGER_AS_FILLER:
            br = cfg.get("brainrot", {})
            top_frac = br.get("webcam_top_frac", 0.5) if layout == "brainrot" else 0.5
            cam_h  = int(oh * top_frac)
            fill_h = oh - cam_h
            if layout == "brainrot":
                cx, cy, cw, ch = crop_mod._resolve_cam(cfg, scene, crop_override)
                cam_chain = (
                    f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
                    f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{cam_h}[cam]"
                )
            else:  # screen_filler: gameplay top, stinger bottom
                gc = _gcrop()
                cam_chain = (
                    f"[0:v]crop={gc},"
                    f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{cam_h}[cam]"
                )
            fill_chain = (
                f"[1:v]scale={ow}:{fill_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{fill_h}[fill]"
            )
            stack = "[fill][cam]vstack=inputs=2[v]" if (swap and layout == "brainrot") \
                    else "[cam][fill]vstack=inputs=2[v]"
            filt = f"{cam_chain};{fill_chain};{stack}"

        else:  # watch_party
            sc_c    = cfg["layout"]["scenes"].get("scene_c", {})
            gbox    = sc_c.get("gameplay", {"x": 0.27, "y": 0.0, "w": 0.50, "h": 1.0})
            gx = int(gbox.get("x", 0.27) * sw); gy = int(gbox.get("y", 0.0) * sh)
            gw = int(gbox.get("w", 0.50) * sw); gh = int(gbox.get("h", 1.0) * sh)
            cam_h  = int(oh * 0.40)
            game_h = oh - cam_h
            filt = (
                f"[1:v]scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{cam_h}[top];"
                f"[0:v]crop={gw}:{gh}:{gx}:{gy},"
                f"scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{game_h}[bot];"
                f"[top][bot]vstack=inputs=2[v]"
            )

        if ol_slot is not None:
            filt += f";[v][{ol_slot}:v]overlay=0:0:format=auto,setsar=1,format=yuv420p[vout]"
            map_v = "[vout]"
        else:
            filt += ";[v]format=yuv420p[vout2]"
            map_v = "[vout2]"

        _run_ffmpeg([
            "ffmpeg", "-y",
            *inputs,
            "-t", f"{stinger_dur:.3f}",
            "-filter_complex", filt,
            "-map", map_v,
            "-map", "0:a",
            "-r", str(fps),
            *_video_enc_args(cfg),
            "-c:a", "aac", "-b:a", "192k", "-ac", "1",
            "-movflags", "+faststart",
            str(out_path),
        ])

    else:
        # The stinger fills the same visual slot as the main clip content.
        #
        # gameplay_fill: game content occupies 1080×H_slot (centered, blur bars
        #   top+bottom). H_slot is derived from the game crop's aspect ratio in
        #   the OBS source.  The stinger fills that same 1080×H_slot slot (zoomed
        #   to cover, center-cropped) with the same blur bars — giving identical
        #   frame composition to the main clip.
        #
        # gameplay_zoom / everything else: game fills the full frame, so stinger
        #   also fills full frame using full_frame blur-fill.
        if layout in ("gameplay_fill", "gameplay_black", "gameplay_original") and game_override is not None:
            src_w, src_h = cfg["layout"]["source_resolution"]
            crop_w_px = game_override["w"] * src_w
            crop_h_px = game_override["h"] * src_h
            content_scale = min(ow / crop_w_px, oh / crop_h_px)
            h_slot = min(oh, int(crop_h_px * content_scale) & ~1)
            if layout == "gameplay_black":
                _fps = cfg["output"]["fps"]
                layout_filt = (
                    f"[0:v]scale={ow}:{h_slot}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{h_slot}[stng_fg];"
                    f"color=c=black:s={ow}x{oh}:r={_fps}[stng_bg];"
                    f"[stng_bg][stng_fg]overlay=(W-w)/2:(H-h)/2[v]"
                )
            elif layout == "gameplay_original":
                # Original full frame blurred to fill background, stinger centered
                layout_filt = (
                    f"[0:v]split=2[stng_bg][stng_fg];"
                    f"[stng_bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{oh},boxblur=40:2[go_bg];"
                    f"[stng_fg]scale={ow}:{h_slot}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{h_slot}[go_fg];"
                    f"[go_bg][go_fg]overlay=(W-w)/2:(H-h)/2[v]"
                )
            else:
                # gameplay_fill: blur bars around stinger in the same slot
                layout_filt = (
                    f"[0:v]split=2[stng_bg][stng_fg];"
                    f"[stng_bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{oh},boxblur=40:2[bgb];"
                    f"[stng_fg]scale={ow}:{h_slot}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{h_slot}[fg_slot];"
                    f"[bgb][fg_slot]overlay=(W-w)/2:(H-h)/2[v]"
                )
        else:
            layout_filt = crop_mod.build_full_frame_filter(cfg)

        inputs = ["-i", str(stinger_path)]
        if overlay_path:
            inputs += ["-loop", "1", "-i", str(overlay_path)]
            _ov_y = int(cfg.get("overlay", {}).get("y_offset", 0))
            filt = layout_filt + (
                f";[v][1:v]overlay=0:{_ov_y}:format=auto,setsar=1,format=yuv420p[vout]"
            )
            map_v = "[vout]"
        else:
            filt = layout_filt + ";[v]format=yuv420p[vout2]"
            map_v = "[vout2]"

        _run_ffmpeg([
            "ffmpeg", "-y",
            *inputs,
            "-t", f"{stinger_dur:.3f}",
            "-filter_complex", filt,
            "-map", map_v,
            "-map", "0:a",
            "-r", str(fps),
            *_video_enc_args(cfg),
            "-c:a", "aac", "-b:a", "192k", "-ac", "1",
            "-movflags", "+faststart",
            str(out_path),
        ])


def _concat_clips(clip_a: Path, clip_b: Path, out_path: Path, cfg: dict) -> None:
    """Concatenate two encoded clips into one using the concat filter."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(clip_a), "-i", str(clip_b),
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        *_video_enc_args(cfg),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ])


def render_raw_clip(vod: Path, start: float, end: float, out_path: Path) -> Path:
    """Stream-copy cut from the source VOD — no crop, no encode, no overlays."""
    pts_off = vod_pts_offset(vod)
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-ss", f"{start + pts_off:.3f}",
        "-i", str(vod),
        "-t", f"{end - start:.3f}",
        "-c", "copy",
        str(out_path),
    ])
    return out_path


def _build_cuts_filter(duration: float,
                        cuts: list[dict]) -> tuple[list[str], float, list[float]]:
    """Return (extra filter stages, kept duration, splice boundary times) for
    jump cuts.

    cuts = [{start, end}, ...] clip-relative seconds to REMOVE.
    Output labels: [v_cut] (video), [a_cut] (audio).
    Boundary times are cumulative positions (seconds, in the OUTPUT/kept
    timeline) where two unrelated segments get spliced together by concat —
    callers use these to force a keyframe there (see render_clip).
    """
    segs: list[tuple[float, float]] = []
    cur = 0.0
    for c in sorted(cuts, key=lambda x: x["start"]):
        s, e = float(c["start"]), float(c["end"])
        if s > cur + 0.01:
            segs.append((cur, s))
        cur = max(cur, e)
    if cur < duration - 0.01:
        segs.append((cur, duration))
    if not segs:
        return [], 0.0, []

    stages: list[str] = []
    kept = sum(e - s for s, e in segs)

    if len(segs) == 1:
        # Single kept segment: plain trim — avoids split=1 / concat=n=1 which
        # some ffmpeg builds reject.
        s, e = segs[0]
        stages.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v_cut]"
        )
        stages.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a_cut]"
        )
        return stages, kept, []

    n = len(segs)

    vsplit = "".join(f"[vs{i}]" for i in range(n))
    stages.append(f"[0:v]split={n}{vsplit}")
    for i, (s, e) in enumerate(segs):
        stages.append(
            f"[vs{i}]trim=start={s:.3f}:end={e:.3f},"
            f"setpts=PTS-STARTPTS[vc{i}]"
        )
    stages.append("".join(f"[vc{i}]" for i in range(n))
                  + f"concat=n={n}:v=1:a=0[v_cut]")

    # Jump cuts splice unrelated audio samples back-to-back; without a fade at
    # each seam the waveform jumps discontinuously and produces an audible
    # click/pop at every cut. A short (15ms) fade in/out on every kept segment
    # is inaudible as a level change but guarantees zero-crossing at the
    # boundary, eliminating the click.
    _DECLICK_FADE = 0.015
    asplit = "".join(f"[as{i}]" for i in range(n))
    stages.append(f"[0:a]asplit={n}{asplit}")
    for i, (s, e) in enumerate(segs):
        seg_dur = e - s
        fade = min(_DECLICK_FADE, seg_dur / 2)
        stages.append(
            f"[as{i}]atrim=start={s:.3f}:end={e:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade:.4f},"
            f"afade=t=out:st={seg_dur - fade:.4f}:d={fade:.4f}[ac{i}]"
        )
    stages.append("".join(f"[ac{i}]" for i in range(n))
                  + f"concat=n={n}:v=0:a=1[a_cut]")

    # Cumulative kept-duration at each internal splice (i.e. every seam
    # except the very end) — where concat glues two unrelated moments
    # together with no encoder-visible scene-cut hint.
    boundaries: list[float] = []
    acc = 0.0
    for s, e in segs[:-1]:
        acc += e - s
        boundaries.append(round(acc, 3))

    return stages, kept, boundaries


def render_clip(vod: Path, start: float, end: float,
                layout: str, ass_path: Path,
                out_path: Path, cfg: dict,
                scene: str = "scene_a",
                chat_overlay_png: Path | None = None,
                chat_overlay_start: float = 0.0,
                chat_overlay_duration: float = 5.0,
                chat_overlays: list | None = None,  # [(Path, start_t, dur), ...]
                filler_path: Path | None = None,
                filler_start: float = 0.0,
                filler_loop: bool = False,
                music_on: bool = False,
                music_seed: int | None = None,
                music_file: Path | None = None,
                project_root: Path | None = None,
                crop_override: dict | None = None,
                face_override: dict | None = None,
                subs_override: dict | None = None,
                swap: bool = False,
                game_override: dict | None = None,
                game_override2: dict | None = None,
                discord_sound: bool = False,
                censor_words: list | None = None,
                peak_sec: float | None = None,
                emoji_png: Path | None = None,
                emoji_start: float = 0.0,
                emoji_duration: float = 2.5,
                subs_emoji_overlays: list[tuple] | None = None,
                cuts: list[dict] | None = None,
                sfx_triggers: list[dict] | None = None,
                title_png: Path | None = None,
                stinger_path: Path | None = None) -> Path:
    ow, oh = cfg["output"]["resolution"]
    fps = cfg["output"]["fps"]
    crf = cfg["output"]["crf"]
    duration = end - start
    _pts_off = vod_pts_offset(vod)

    # Two-step seek: fast seek to 10 s before the clip, then trim accurately
    # in the filter so the output PTS starts at exactly 0 = clip start.
    # This removes the need for a subtitle seek-offset correction.
    _seek_target = start + _pts_off
    _pre_sec     = min(10.0, _seek_target)   # how far we fast-seek before clip start
    _coarse_seek = _seek_target - _pre_sec

    # ── Assemble ffmpeg inputs and track which slot is which ──
    # +genpts regenerates presentation timestamps from decode order, which
    # papers over the non-monotonic DTS Twitch VODs sometimes have around ad
    # breaks — without it ffmpeg's filter graph can stall indefinitely there.
    inputs: list[str] = ["-fflags", "+genpts", "-ss", f"{_coarse_seek:.3f}", "-i", str(vod)]
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
                                       music_file=music_file,
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

    # Resolve chat overlays: list takes priority over single PNG
    _chat_overlays: list[tuple] = []
    if chat_overlays:
        _chat_overlays = list(chat_overlays)
    elif chat_overlay_png is not None:
        _chat_overlays = [(chat_overlay_png, chat_overlay_start, chat_overlay_duration)]

    chat_slots: list[tuple[int, float, float]] = []
    for _png, _t0, _tdur in _chat_overlays:
        inputs += ["-loop", "1", "-i", str(_png)]
        chat_slots.append((next_slot, _t0, _tdur))
        next_slot += 1

    emoji_slot = None
    if emoji_png is not None:
        inputs += ["-loop", "1", "-i", str(emoji_png)]
        emoji_slot = next_slot
        next_slot += 1

    subs_emoji_slots: list[tuple[int, float, float, float]] = []  # (slot, start, dur, x_jitter)
    for _png, _t0, _tdur, _xj in (subs_emoji_overlays or []):
        inputs += ["-loop", "1", "-i", str(_png)]
        subs_emoji_slots.append((next_slot, _t0, _tdur, _xj))
        next_slot += 1

    title_slot = None
    if title_png is not None:
        inputs += ["-loop", "1", "-i", str(title_png)]
        title_slot = next_slot
        next_slot += 1

    ping_slot = None
    if discord_sound:
        inputs += ["-f", "lavfi", "-i",
                   "sine=frequency=880:sample_rate=44100:duration=0.25"]
        ping_slot = next_slot
        next_slot += 1

    # One discord notification input per curse word, delayed to its timestamp
    censor_slots: list[int] = []
    _discord_sfx = (project_root / "sfx" / "intro_sfx.mp3") if project_root else None
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

    # SFX triggers: one input per trigger (same file may appear multiple times)
    sfx_slots: list[tuple[int, dict]] = []  # (slot, trigger_dict)
    if sfx_triggers and project_root is not None:
        sfx_dir = project_root / "sfx"
        for trigger in sfx_triggers:
            sfx_file = sfx_dir / trigger["file"]
            if sfx_file.exists():
                inputs += ["-i", str(sfx_file)]
                sfx_slots.append((next_slot, trigger))
                next_slot += 1

    overlay_slot = None
    if project_root is not None:
        _overlay_png = project_root / "overlay" / "renderoverlaytiktok.png"
        if _overlay_png.exists():
            inputs += ["-loop", "1", "-i", str(_overlay_png)]
            overlay_slot = next_slot
            next_slot += 1

    # ── Accurate-seek trim: normalise PTS so that t=0 in the filter = clip start.
    # We fast-seeked _pre_sec before the clip; trim that pre-roll away and reset
    # PTS so downstream filters (especially the ass subtitle filter) see t=0 at
    # the exact first frame of the clip.  This replaces [0:v]/[0:a] with
    # [v_src]/[a_src] throughout the rest of the filter chain.
    #
    # apad guarantees a_src is never shorter than the video: the source VOD's
    # audio track sometimes runs a hair short of the video for a given span
    # (Twitch VOD stitching / dropped audio frames near ad-break seams), and
    # without padding that leaves the tail of the clip silent once muxed
    # against the full-length video track.
    _clip_dur = end - start   # original full duration before any jump-cuts
    _src_trim = (
        f"[0:v]trim=start={_pre_sec:.3f}:end={_pre_sec + _clip_dur:.3f},"
        f"setpts=PTS-STARTPTS[v_src]"
        f";[0:a]atrim=start={_pre_sec:.3f}:end={_pre_sec + _clip_dur:.3f},"
        f"asetpts=PTS-STARTPTS,apad=whole_dur={_clip_dur:.3f}[a_src]"
    )

    # ── Jump cuts: split/trim/concat before layout ──
    cut_stages: list[str] = []
    cut_boundaries: list[float] = []
    if cuts:
        cut_stages, duration, cut_boundaries = _build_cuts_filter(duration, cuts)
        # _build_cuts_filter uses [0:v]/[0:a]; rewrite to our normalised labels
        cut_stages = [s.replace("[0:v]", "[v_src]").replace("[0:a]", "[a_src]")
                      for s in cut_stages]
        if cut_stages:
            # concat's audio output has irregular frame chunking around the
            # asetpts-reset segment boundaries. Handing that straight to
            # loudnorm confuses the final aresample=async's gap detection
            # (see below), which then aggressively stretches/skips samples
            # for the first ~2.5s of the WHOLE clip — not just near a cut
            # boundary — audible as scrambled/stuttering audio right at the
            # start. Re-syncing here, before loudnorm ever sees the concat
            # output, avoids the interaction entirely.
            cut_stages.append(
                "[a_cut]aresample=async=1:min_hard_comp=0.100000:"
                "first_pts=0[a_cut_rs]"
            )

    # ── Build the video filter chain ──
    layout_filter = crop_mod.build_filter(cfg, layout, scene,
                                          crop_override=crop_override,
                                          face_override=face_override,
                                          game_override=game_override,
                                          game_override2=game_override2,
                                          swap=swap)
    if cuts and cut_stages:
        layout_filter = layout_filter.replace("[0:v]", "[v_cut]")
        audio_plan.filter_chain = audio_plan.filter_chain.replace("[0:a]", "[a_cut_rs]")
    else:
        layout_filter = layout_filter.replace("[0:v]", "[v_src]")
        audio_plan.filter_chain = audio_plan.filter_chain.replace("[0:a]", "[a_src]")

    # The brainrot filter uses [1:v]. If filler_slot != 1, rewrite.
    if filler_slot is not None and filler_slot != 1:
        layout_filter = layout_filter.replace("[1:v]", f"[{filler_slot}:v]")

    stages = [_src_trim] + cut_stages + [layout_filter]
    current = "[v]"

    # ── Zoom punch-in at peak second ──
    zoom_cfg = cfg.get("zoom_punch", {})
    if zoom_cfg.get("enabled", False) and peak_sec is not None:
        peak_t = peak_sec - start
        zm = float(zoom_cfg.get("zoom_amount", 0.07))
        sigma = float(zoom_cfg.get("sigma", 8.0))
        t_safe = "if(isnan(t),0,t)"
        z = f"(1+{zm:.3f}*exp(-{sigma:.1f}*({t_safe}-{peak_t:.3f})^2))"
        stages.append(
            f"{current}crop=w='iw/{z}':h='ih/{z}':"
            f"x='(iw-iw/{z})/2':y='(ih-ih/{z})/2',"
            f"scale={ow}:{oh}[vzoom]"
        )
        current = "[vzoom]"

    # ── Emoji reaction overlay ──
    if emoji_slot is not None:
        ec = cfg.get("emoji_overlay", {})
        px = float(ec.get("position_x_frac", 0.82))
        py = float(ec.get("position_y_frac", 0.22))
        fade = float(ec.get("fade_sec", 0.25))
        t0 = emoji_start
        t1 = emoji_start + emoji_duration
        stages.append(
            f"[{emoji_slot}:v]format=rgba,"
            f"fade=t=in:st={t0:.2f}:d={fade}:alpha=1,"
            f"fade=t=out:st={max(t0, t1 - fade):.2f}:d={fade}:alpha=1[emoji_v]"
        )
        stages.append(
            f"{current}[emoji_v]overlay=x=W*{px:.3f}-w/2:y=H*{py:.3f}-h/2:"
            f"enable='between(t,{t0:.2f},{t1:.2f})'[vemoji]"
        )
        current = "[vemoji]"

    # ── Title overlay (static PNG, full clip duration) ──
    if title_slot is not None:
        title_y_frac = float(cfg.get("subs", {}).get("title_position_y_frac", 0.15))
        stages.append(f"[{title_slot}:v]format=rgba[title_v]")
        stages.append(
            f"{current}[title_v]overlay=x=(W-w)/2:y=H*{title_y_frac:.3f}-h/2:format=auto[vtitle]"
        )
        current = "[vtitle]"

    if chat_slots:
        cc = cfg.get("chat_overlay", {})
        pos_y_frac = cc.get("position_y_frac", 0.12)
        fade = cc.get("fade_sec", 0.25)
        for ci, (cslot, t0, tdur) in enumerate(chat_slots):
            t1 = t0 + tdur
            lbl_in  = f"[chat{ci}]"
            lbl_out = f"[vo{ci}]"
            stages.append(
                f"[{cslot}:v]format=rgba,"
                f"fade=t=in:st={t0:.3f}:d={fade}:alpha=1,"
                f"fade=t=out:st={max(t0, t1 - fade):.3f}:d={fade}:alpha=1{lbl_in}"
            )
            stages.append(
                f"{current}{lbl_in}overlay=x=(W-w)/2:y=H*{pos_y_frac}-h/2:"
                f"enable='between(t,{t0:.3f},{t1:.3f})':format=auto{lbl_out}"
            )
            current = lbl_out

    # ── Subtitle emoji theme: random emoji pops beside the spoken word ──
    if subs_emoji_slots:
        ec = cfg.get("subs_emoji_theme", {})
        subs_cfg = cfg.get("subs", {})
        subs_y_frac = float(subs_cfg.get("position_y_frac", 0.73))
        if subtitle_mod.get_style(subs_cfg) == "word_pop":
            # Inline beside the word: nudge up slightly since ASS anchors
            # text at its bottom edge, not its vertical centre.
            y_nudge = float(ec.get("y_nudge_frac", 0.035))
            y_frac = max(0.02, subs_y_frac - y_nudge)
        else:
            # Karaoke's active word position within the line isn't known
            # here, so fall back to floating above the caption.
            gap_above = float(ec.get("fallback_gap_above_subs_frac", 0.12))
            y_frac = max(0.02, subs_y_frac - gap_above)
        pop_amount = float(ec.get("pop_amount", 0.45))
        pop_sigma = float(ec.get("pop_sigma", 40.0))
        fade = float(ec.get("fade_sec", 0.08))
        for si, (sslot, t0, tdur, xj) in enumerate(subs_emoji_slots):
            t0 = max(0.0, t0)   # ffmpeg's fade filter rejects a negative st
            t1 = t0 + tdur
            t_safe = "if(isnan(t),0,t)"
            pop_expr = f"(1+{pop_amount:.3f}*exp(-{pop_sigma:.1f}*({t_safe}-{t0:.3f})^2))"
            lbl_in = f"[spe{si}]"
            lbl_out = f"[spo{si}]"
            stages.append(
                f"[{sslot}:v]format=rgba,"
                f"scale=w='trunc(iw*{pop_expr}/2)*2':h='trunc(ih*{pop_expr}/2)*2':"
                f"eval=frame:flags=lanczos,"
                f"fade=t=in:st={t0:.3f}:d={fade}:alpha=1,"
                f"fade=t=out:st={max(t0, t1 - fade):.3f}:d={fade}:alpha=1{lbl_in}"
            )
            stages.append(
                f"{current}{lbl_in}overlay=x=(W-w)/2+W*{xj:.3f}:y=H*{y_frac:.3f}-h/2:"
                f"enable='between(t,{t0:.3f},{t1:.3f})':format=auto{lbl_out}"
            )
            current = lbl_out

    # ffmpeg 8.x requires explicit filename= for the ass filter.
    # Use forward slashes (as_posix()) even on Windows: escaping backslashes
    # for the filtergraph parser (\ -> \\) is technically correct per ffmpeg's
    # docs but its filter-option parser chokes on the doubled backslashes in
    # practice (`No option name near ...`) — forward slashes sidestep that
    # entirely and Windows accepts them natively. The drive-letter colon still
    # needs escaping, and needs it TWICE (\\:) — the ass filter's own option
    # parser and the outer filtergraph parser each unescape one level.
    # Verified against ffmpeg 9.0 on Windows: single '\:' fails with
    # "No option name near ..."; '\\:' works.
    esc = ass_path.as_posix().replace(':', '\\\\:').replace("'", "\\'")
    if overlay_slot is not None:
        _ov_y = int(cfg.get("overlay", {}).get("y_offset", 0))
        stages.append(f"{current}ass=filename={esc},setsar=1[vpre_overlay]")
        stages.append(
            f"[vpre_overlay][{overlay_slot}:v]overlay=0:{_ov_y}:format=auto,"
            f"setsar=1,format=yuv420p[vout]"
        )
    else:
        stages.append(f"{current}ass=filename={esc},setsar=1,format=yuv420p[vout]")

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
            beep_vol = float(cfg.get("censor", {}).get("beep_volume", 0.35))
            beep_labels = []
            for i, (slot, word) in enumerate(zip(censor_slots, censor_words)):
                delay_ms = int(word["start"] * 1000)
                full_filter += f";[{slot}:a]adelay={delay_ms}|{delay_ms},volume={beep_vol}[_beep{i}]"
                beep_labels.append(f"[_beep{i}]")
            n = 1 + len(beep_labels)
            full_filter += (
                f";[_cmuted]{''.join(beep_labels)}"
                f"amix=inputs={n}:duration=first:normalize=0:dropout_transition=0[_aout_censor]"
            )
            final_audio = "[_aout_censor]"

    if sfx_slots:
        sfx_labels = []
        for i, (slot, trigger) in enumerate(sfx_slots):
            delay_ms = int(float(trigger["start"]) * 1000)
            vol = float(trigger.get("volume", 1.0))
            full_filter += (
                f";[{slot}:a]adelay={delay_ms}|{delay_ms},"
                f"volume={vol}[_sfx{i}]"
            )
            sfx_labels.append(f"[_sfx{i}]")
        n = 1 + len(sfx_labels)
        full_filter += (
            f";{final_audio}{''.join(sfx_labels)}"
            f"amix=inputs={n}:duration=first:normalize=0:dropout_transition=0[_aout_sfx]"
        )
        final_audio = "[_aout_sfx]"

    # Twitch VODs occasionally carry non-monotonic audio DTS around ad-break
    # discontinuities (same root cause as the video stall guarded above).
    # +genpts and asetpts fix the audio's own PTS at trim time, but the
    # loudnorm/amix chain can still hand the AAC encoder samples with
    # irregular spacing, producing "Non-monotonic DTS" / "Queue input is
    # backward in time" at mux time and audible glitches in the output.
    # aresample=async=1 re-syncs the stream to a strictly monotonic clock
    # (inserting/dropping samples as needed) right before encoding.
    # first_pts=0 anchors the timeline at the very first sample instead of
    # whatever PTS the loudnorm/amix chain happens to report there — without
    # it, async perceives a (spurious) large gap at t=0 and aggressively
    # stretches audio to "catch up", audible as speed-up + stutter for the
    # first few seconds until it settles. min_hard_comp=0.1 keeps any real
    # correction to smooth stretching instead of an audible hard sample
    # skip/insert unless the drift exceeds 100ms.
    full_filter += (
        f";{final_audio}aresample=async=1:min_hard_comp=0.100000:"
        f"first_pts=0[_afinal]"
    )
    final_audio = "[_afinal]"

    # ── Encoder ──
    video_enc_args = _video_enc_args(cfg, has_cuts=bool(cut_boundaries))

    # If a stinger is requested, render the main clip to a temp file first,
    # then concat the stinger segment after.
    render_target = out_path
    tmp_main = tmp_stinger = None
    if stinger_path is not None and stinger_path.exists():
        tmp_main    = out_path.with_suffix(".stmp_main.mp4")
        tmp_stinger = out_path.with_suffix(".stmp_stinger.mp4")
        render_target = tmp_main

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-t", f"{duration:.3f}",
        "-filter_complex", full_filter,
        "-map", "[vout]",
        "-map", final_audio,
        "-r", str(fps),
        *video_enc_args,
    ]
    if cut_boundaries:
        # Jump cuts splice two unrelated moments together with concat; unlike
        # libx264 (which auto-detects the scene change via scenecut= and
        # inserts an I-frame there), h264_nvenc doesn't, so the first frames
        # after a cut get predicted from content that no longer resembles
        # them — visible as a blocky/stuttery beat right at the cut. Forcing
        # a keyframe exactly at each splice point fixes this for every
        # encoder, not just nvenc.
        cmd += ["-force_key_frames", ",".join(f"{b:.3f}" for b in cut_boundaries)]
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(render_target),
    ]
    _run_ffmpeg(cmd)

    if tmp_main is not None and tmp_stinger is not None:
        try:
            _render_stinger_segment(
                stinger_path, layout, cfg, tmp_stinger,
                vod=vod, vod_end=end, scene=scene,
                crop_override=crop_override, face_override=face_override,
                game_override=game_override, game_override2=game_override2,
                swap=swap, project_root=project_root,
            )
            _concat_clips(tmp_main, tmp_stinger, out_path, cfg)
        finally:
            tmp_main.unlink(missing_ok=True)
            tmp_stinger.unlink(missing_ok=True)

    return out_path
