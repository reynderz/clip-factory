"""Build the ffmpeg filter_complex string for 9:16 output.

Output layouts (the visual final form):
  - "split"        : webcam + gameplay (only valid if scene_a — has gameplay)
  - "webcam_only"  : full-frame webcam with blurred-fill background
  - "brainrot"     : webcam + filler video from input [1:v]

The webcam and gameplay coordinates come from the DETECTED SCENE, not from
a global block. We pass `scene` ('scene_a' or 'scene_b') in.
"""
from __future__ import annotations


def _px(box: dict, sw: int, sh: int) -> tuple[int, int, int, int]:
    return (int(box["x"] * sw), int(box["y"] * sh),
            int(box["w"] * sw), int(box["h"] * sh))


def _scene_boxes(cfg: dict, scene: str) -> tuple[dict, dict | None]:
    s = cfg["layout"]["scenes"][scene]
    return s["webcam"], s.get("gameplay")


def _resolve_cam(cfg: dict, scene: str,
                  crop_override: dict | None) -> tuple[int, int, int, int]:
    """Return (cx, cy, cw, ch) in pixels, using override if provided."""
    sw, sh = cfg["layout"]["source_resolution"]
    if crop_override is not None:
        cx = int(crop_override["x"] * sw)
        cy = int(crop_override["y"] * sh)
        cw = int(crop_override["w"] * sw)
        ch = int(crop_override["h"] * sh)
        return cx, cy, cw, ch
    cam_box, _ = _scene_boxes(cfg, scene)
    return _px(cam_box, sw, sh)


def build_split_filter(cfg: dict, scene: str,
                       crop_override: dict | None = None,
                       game_override: dict | None = None) -> str:
    """Webcam on top, gameplay on bottom. Requires scene with gameplay.

    When crop_override is given, it overrides the webcam (top) region only.
    When game_override is given, it overrides the gameplay region only.
    """
    sw, sh = cfg["layout"]["source_resolution"]
    ow, oh = cfg["output"]["resolution"]
    _, game_box = _scene_boxes(cfg, scene)
    if game_box is None:
        # Caller asked for split on a no-gameplay scene; fall back to webcam_only
        return build_webcam_only_filter(cfg, scene, crop_override=crop_override)

    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    gx, gy, gw, gh = _px(game_box, sw, sh)
    if game_override is not None:
        gx = int(game_override["x"] * sw)
        gy = int(game_override["y"] * sh)
        gw = int(game_override["w"] * sw)
        gh = int(game_override["h"] * sh)
    cam_h = int(oh * 0.45)
    game_h = oh - cam_h

    return (
        f"[0:v]split=2[a][b];"
        f"[a]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h}[cam];"
        f"[b]crop={gw}:{gh}:{gx}:{gy},split=2[g_bg][g_fg];"
        f"[g_bg]scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{game_h},boxblur=30:2[g_bgb];"
        f"[g_fg]scale={ow}:{game_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[g_fgs];"
        f"[g_bgb][g_fgs]overlay=(W-w)/2:(H-h)/2[game];"
        f"[cam][game]vstack=inputs=2[v]"
    )


def _face_center(cfg: dict, scene: str,
                  face_override: dict | None = None) -> tuple[float, float]:
    if face_override is not None:
        return face_override.get("x", 0.5), face_override.get("y", 0.5)
    s = cfg["layout"]["scenes"].get(scene, {})
    return s.get("face_center_x", 0.5), s.get("face_center_y", 0.5)


def _centered_source_crop(cfg: dict, scene: str,
                           crop_override: dict | None,
                           face_override: dict | None) -> tuple[int, int, int, int]:
    """Source region (sx, sy, sw, sh) for the centered layout.

    At zoom=1 this is the minimal 9:16-mapped sub-region of the webcam
    (same pixels as the old face_center final-crop approach).
    At zoom>1 it zooms into a proportionally smaller area, keeping
    face_center_x/y as the anchor point.
    """
    ow, oh = cfg["output"]["resolution"]
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    fx, fy = _face_center(cfg, scene, face_override)
    zoom = max(1.0, (face_override or {}).get("zoom", 1.0))
    base_scale = max(ow / cw, oh / ch)
    vis_w = min(cw, ow / (base_scale * zoom))
    vis_h = min(ch, oh / (base_scale * zoom))
    sx = cx + int((cw - vis_w) * fx)
    sy = cy + int((ch - vis_h) * fy)
    return sx, sy, max(2, int(vis_w)), max(2, int(vis_h))


def _gaussian_source_crop(cfg: dict, scene: str,
                           crop_override: dict | None,
                           face_override: dict | None) -> tuple[int, int, int, int]:
    """Source region for the gaussian layout.

    At zoom=1 this is the full webcam (preserving the classic gaussian look
    with blur bars). At zoom>1 it crops into a smaller area.
    """
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    fx, fy = _face_center(cfg, scene, face_override)
    zoom = max(1.0, (face_override or {}).get("zoom", 1.0))
    sw = max(2, int(cw / zoom))
    sh = max(2, int(ch / zoom))
    sx = cx + int((cw - sw) * fx)
    sy = cy + int((ch - sh) * fy)
    return sx, sy, sw, sh


def build_centered_filter(cfg: dict, scene: str,
                           crop_override: dict | None = None,
                           face_override: dict | None = None) -> str:
    """Fill 9:16 with no blur, no bars. Zoom + face_center select the source crop."""
    ow, oh = cfg["output"]["resolution"]
    sx, sy, sw, sh = _centered_source_crop(cfg, scene, crop_override, face_override)
    return (
        f"[0:v]crop={sw}:{sh}:{sx}:{sy},"
        f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh}[v]"
    )


def build_webcam_only_filter(cfg: dict, scene: str,
                              crop_override: dict | None = None,
                              face_override: dict | None = None) -> str:
    """Blurred-fill background with face centered. Zoom shrinks the source region."""
    ow, oh = cfg["output"]["resolution"]
    sx, sy, sw, sh = _gaussian_source_crop(cfg, scene, crop_override, face_override)

    return (
        f"[0:v]crop={sw}:{sh}:{sx}:{sy},split=2[bg][fg];"
        f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},boxblur=40:2[bgb];"
        f"[fg]scale={ow}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)*0.5:(H-h)*0.5[v]"
    )


def build_brainrot_filter(cfg: dict, scene: str,
                           crop_override: dict | None = None) -> str:
    """Webcam (from streamer) on one half, filler video [1:v] on the other.

    Always uses the webcam region of whatever scene is active — so on a
    Just-Chatting moment you get a tall portrait of the streamer above
    the parkour, and on a Gaming moment you get the small bottom-right
    webcam scaled up over the parkour.
    """
    ow, oh = cfg["output"]["resolution"]
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)

    br = cfg.get("brainrot", {})
    top_frac = br.get("webcam_top_frac", 0.5)
    swap = br.get("swap", False)

    cam_h = int(oh * top_frac)
    fill_h = oh - cam_h

    cam_chain = (
        f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h}[cam]"
    )
    fill_chain = (
        f"[1:v]scale={ow}:{fill_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{fill_h}[fill]"
    )
    stack = (f"[fill][cam]vstack=inputs=2[v]" if swap
             else f"[cam][fill]vstack=inputs=2[v]")
    return f"{cam_chain};{fill_chain};{stack}"


def build_screen_cam_filter(cfg: dict, scene: str,
                             crop_override: dict | None = None) -> str:
    """Full OBS stream frame on top, face-cam crop on bottom."""
    ow, oh = cfg["output"]["resolution"]
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    screen_h = int(oh * 0.5)
    cam_h = oh - screen_h
    return (
        f"[0:v]split=2[scr][cam];"
        f"[scr]scale={ow}:{screen_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{screen_h}[screen];"
        f"[cam]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h}[face];"
        f"[screen][face]vstack=inputs=2[v]"
    )


def build_cam_screen_filter(cfg: dict, scene: str,
                             crop_override: dict | None = None) -> str:
    """Face-cam crop on top, full OBS stream frame on bottom."""
    ow, oh = cfg["output"]["resolution"]
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    cam_h = int(oh * 0.5)
    screen_h = oh - cam_h
    return (
        f"[0:v]split=2[cam][scr];"
        f"[cam]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h}[face];"
        f"[scr]scale={ow}:{screen_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{screen_h}[screen];"
        f"[face][screen]vstack=inputs=2[v]"
    )


def build_screen_gameplay_filter(cfg: dict, scene: str,
                                  crop_override: dict | None = None) -> str:
    """Full OBS stream frame on top, isolated gameplay region on bottom.

    Top half  : the whole 16:9 source frame scaled to fill the output width
                (center-cropped to fit 9:16 half-height).
    Bottom half: the gameplay region cropped from the source and scaled to
                 fill the output width.

    Falls back to webcam_only when the scene has no gameplay region (scene_b).
    """
    sw, sh = cfg["layout"]["source_resolution"]
    ow, oh = cfg["output"]["resolution"]
    _, game_box = _scene_boxes(cfg, scene)
    if game_box is None:
        return build_webcam_only_filter(cfg, scene, crop_override=crop_override)

    gx, gy, gw, gh = _px(game_box, sw, sh)
    screen_h = int(oh * 0.5)
    game_h = oh - screen_h

    return (
        f"[0:v]split=2[scr][gm];"
        f"[scr]scale={ow}:{screen_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{screen_h}[screen];"
        f"[gm]crop={gw}:{gh}:{gx}:{gy},split=2[gm_bg][gm_fg];"
        f"[gm_bg]scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{game_h},boxblur=30:2[gm_bgb];"
        f"[gm_fg]scale={ow}:{game_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[gm_fgs];"
        f"[gm_bgb][gm_fgs]overlay=(W-w)/2:(H-h)/2[game];"
        f"[screen][game]vstack=inputs=2[v]"
    )


def build_watch_party_filter(cfg: dict, scene: str,
                              swap: bool = False,
                              crop_override: dict | None = None) -> str:
    """Watch-party layout: webcam on top (40%), video content on bottom (60%).

    When swap=True the order is reversed: video on top, webcam on bottom.
    Uses scene_c's webcam and gameplay regions; falls back to webcam_only for
    clips that are not classified as scene_c.
    """
    sw, sh = cfg["layout"]["source_resolution"]
    ow, oh = cfg["output"]["resolution"]

    scenes_cfg = cfg["layout"]["scenes"]
    if "scene_c" not in scenes_cfg:
        return build_webcam_only_filter(cfg, scene, crop_override=crop_override)

    sc = scenes_cfg["scene_c"]
    cam_box  = sc["webcam"]
    game_box = sc["gameplay"]

    cx, cy, cw, ch = _px(cam_box,  sw, sh)
    gx, gy, gw, gh = _px(game_box, sw, sh)

    if crop_override is not None:
        cx = int(crop_override["x"] * sw)
        cy = int(crop_override["y"] * sh)
        cw = int(crop_override["w"] * sw)
        ch = int(crop_override["h"] * sh)

    cam_h  = int(oh * 0.40)   # webcam gets 40% of height
    game_h = oh - cam_h        # video content gets 60%

    cam_chain = (
        f"crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h}"
    )
    game_chain = (
        f"crop={gw}:{gh}:{gx}:{gy},"
        f"scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{game_h}"
    )

    if not swap:
        top_chain, top_label = cam_chain,  "[top]"
        bot_chain, bot_label = game_chain, "[bot]"
        top_h, bot_h = cam_h, game_h
    else:
        top_chain, top_label = game_chain, "[top]"
        bot_chain, bot_label = cam_chain,  "[bot]"
        top_h, bot_h = game_h, cam_h

    # Scale values embedded in chains need to match the swap
    if swap:
        top_chain = (
            f"crop={gw}:{gh}:{gx}:{gy},"
            f"scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
            f"crop={ow}:{game_h}"
        )
        bot_chain = (
            f"crop={cw}:{ch}:{cx}:{cy},"
            f"scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
            f"crop={ow}:{cam_h}"
        )

    return (
        f"[0:v]split=2[wp_a][wp_b];"
        f"[wp_a]{top_chain}[top];"
        f"[wp_b]{bot_chain}[bot];"
        f"[top][bot]vstack=inputs=2[v]"
    )


def build_facecam_only_filter(cfg: dict, scene: str,
                               crop_override: dict | None = None) -> str:
    """Facecam region scaled to fill the full 9:16 frame — no blur bars."""
    ow, oh = cfg["output"]["resolution"]
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    return (
        f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh}[v]"
    )


def build_full_frame_filter(cfg: dict) -> str:
    """Full source frame scaled to 9:16 with blurred-fill background."""
    ow, oh = cfg["output"]["resolution"]
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},boxblur=40:2[bgb];"
        f"[fg]scale={ow}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)*0.5:(H-h)*0.5[v]"
    )


def build_screen_only_filter(cfg: dict, scene: str) -> str:
    """Gameplay/screen region scaled to 9:16 with blurred-fill background."""
    sw, sh = cfg["layout"]["source_resolution"]
    ow, oh = cfg["output"]["resolution"]
    _, game_box = _scene_boxes(cfg, scene)
    if game_box is None:
        return build_full_frame_filter(cfg)
    gx, gy, gw, gh = _px(game_box, sw, sh)
    return (
        f"[0:v]crop={gw}:{gh}:{gx}:{gy},split=2[bg][fg];"
        f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},boxblur=40:2[bgb];"
        f"[fg]scale={ow}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)*0.5:(H-h)*0.5[v]"
    )


def _resolve_game(cfg: dict, scene: str,
                   game_override: dict | None) -> tuple[int, int, int, int]:
    """Return pixel coords for the primary (top) game crop."""
    sw, sh = cfg["layout"]["source_resolution"]
    _, game_box = _scene_boxes(cfg, scene)
    if game_override is not None:
        return (int(game_override["x"] * sw), int(game_override["y"] * sh),
                int(game_override["w"] * sw), int(game_override["h"] * sh))
    if game_box is not None:
        return _px(game_box, sw, sh)
    return 0, 0, sw, sh


def _resolve_game2(cfg: dict, scene: str,
                    game_override2: dict | None) -> tuple[int, int, int, int]:
    """Return pixel coords for the secondary (bottom) game crop.

    Falls back to scene gameplay2 then gameplay — never silent duplicate.
    """
    sw, sh = cfg["layout"]["source_resolution"]
    if game_override2 is not None:
        return (int(game_override2["x"] * sw), int(game_override2["y"] * sh),
                int(game_override2["w"] * sw), int(game_override2["h"] * sh))
    game2_box = cfg["layout"]["scenes"].get(scene, {}).get("gameplay2")
    if game2_box is not None:
        return _px(game2_box, sw, sh)
    _, game_box = _scene_boxes(cfg, scene)
    if game_box is not None:
        return _px(game_box, sw, sh)
    return 0, 0, sw, sh


def build_dual_screen_filter(cfg: dict, scene: str,
                              game_override: dict | None = None,
                              game_override2: dict | None = None) -> str:
    """Two independent screen crops stacked 50/50.

    Each half uses blur-fill: the exact selected region is scaled to fit its
    slot (no pixels outside the selection are ever shown), centered on a
    blurred version of itself as background.  This means what you draw in the
    GUI is exactly what renders — no unexpected cropping or shifting.

    Top uses game_override → scene gameplay.
    Bottom uses game_override2 → scene gameplay2 → scene gameplay.
    """
    sw, sh = cfg["layout"]["source_resolution"]
    ow, oh = cfg["output"]["resolution"]
    gx,  gy,  gw,  gh  = _resolve_game(cfg, scene, game_override)
    g2x, g2y, g2w, g2h = _resolve_game2(cfg, scene, game_override2)

    top_h = oh // 2
    bot_h = oh - top_h

    return (
        f"[0:v]split=2[ds_t][ds_b];"

        # ── top half ──
        f"[ds_t]crop={gw}:{gh}:{gx}:{gy},split=2[t_bg_in][t_fg_in];"
        # blurred background: scale-up to cover, blur
        f"[t_bg_in]scale={ow}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{top_h},boxblur=30:2[t_bg];"
        # exact foreground: scale-down to fit, even dimensions
        f"[t_fg_in]scale={ow}:{top_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[t_fg];"
        f"[t_bg][t_fg]overlay=(W-w)/2:(H-h)/2[top];"

        # ── bottom half ──
        f"[ds_b]crop={g2w}:{g2h}:{g2x}:{g2y},split=2[b_bg_in][b_fg_in];"
        f"[b_bg_in]scale={ow}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{bot_h},boxblur=30:2[b_bg];"
        f"[b_fg_in]scale={ow}:{bot_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[b_fg];"
        f"[b_bg][b_fg]overlay=(W-w)/2:(H-h)/2[bot];"

        f"[top][bot]vstack=inputs=2[v]"
    )


def build_screen_filler_filter(cfg: dict, scene: str,
                                game_override: dict | None = None) -> str:
    """Screen/game crop on top (50%), brainrot filler [1:v] on bottom (50%).

    Each half is center-zoomed to fill its portion — no bars.
    """
    ow, oh = cfg["output"]["resolution"]
    gx, gy, gw, gh = _resolve_game(cfg, scene, game_override)
    top_h = oh // 2
    bot_h = oh - top_h
    return (
        f"[0:v]crop={gw}:{gh}:{gx}:{gy},"
        f"scale={ow}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{top_h}[top];"
        f"[1:v]scale={ow}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{bot_h}[bot];"
        f"[top][bot]vstack=inputs=2[v]"
    )


def build_screen_cam_split_filter(cfg: dict, scene: str,
                                   game_override: dict | None = None,
                                   crop_override: dict | None = None) -> str:
    """Screen/game crop on top (50%), facecam gaussian-blur on bottom (50%).

    Each half center-zooms to fill its portion — no bars.
    """
    ow, oh = cfg["output"]["resolution"]
    gx, gy, gw, gh = _resolve_game(cfg, scene, game_override)
    cx, cy, cw, ch = _resolve_cam(cfg, scene, crop_override)
    top_h = oh // 2
    bot_h = oh - top_h
    return (
        f"[0:v]split=2[scs_g][scs_c];"
        f"[scs_g]crop={gw}:{gh}:{gx}:{gy},"
        f"scale={ow}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{top_h}[top];"
        f"[scs_c]crop={cw}:{ch}:{cx}:{cy},split=2[scs_bg][scs_fg];"
        f"[scs_bg]scale={ow}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{bot_h},boxblur=40:2[scs_bgb];"
        f"[scs_fg]scale={ow}:-2:force_original_aspect_ratio=decrease[scs_fgs];"
        f"[scs_bgb][scs_fgs]overlay=(W-w)*0.5:(H-h)*0.5[bot];"
        f"[top][bot]vstack=inputs=2[v]"
    )


def build_crop_fill_filter(cfg: dict, cam: dict, game: dict) -> str:
    """Webcam (top 45%) + gameplay (bottom 55%), each with gaussian blur fill.

    Uses iw/ih expressions — works on any source resolution.
    """
    ow, oh = cfg["output"]["resolution"]
    cam_h  = int(oh * 0.45)
    game_h = oh - cam_h

    def _c(crop):
        return f"iw*{crop['w']}:ih*{crop['h']}:iw*{crop['x']}:ih*{crop['y']}"

    return (
        f"[0:v]split=2[cf_a][cf_b];"

        f"[cf_a]crop={_c(cam)},split=2[ca_bg][ca_fg];"
        f"[ca_bg]scale={ow}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{cam_h},boxblur=40:2[ca_bgb];"
        f"[ca_fg]scale={ow}:{cam_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[ca_fgs];"
        f"[ca_bgb][ca_fgs]overlay=(W-w)/2:(H-h)/2[top];"

        f"[cf_b]crop={_c(game)},split=2[cb_bg][cb_fg];"
        f"[cb_bg]scale={ow}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{game_h},boxblur=40:2[cb_bgb];"
        f"[cb_fg]scale={ow}:{game_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[cb_fgs];"
        f"[cb_bgb][cb_fgs]overlay=(W-w)/2:(H-h)/2[bot];"

        f"[top][bot]vstack=inputs=2[v]"
    )


def build_gameplay_fill_filter(cfg: dict, crop: dict) -> str:
    """Gameplay crop → 9:16 with gaussian blur fill. Uses iw/ih expressions."""
    ow, oh = cfg["output"]["resolution"]
    x, y, w, h = crop["x"], crop["y"], crop["w"], crop["h"]
    return (
        f"[0:v]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},split=2[gf_bg][gf_fg];"
        f"[gf_bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},boxblur=40:2[gf_bgb];"
        f"[gf_fg]scale={ow}:-2:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[gf_fgs];"
        f"[gf_bgb][gf_fgs]overlay=(W-w)*0.5:(H-h)*0.5[v]"
    )


def build_dual_crop_filter(cfg: dict, top: dict, bot: dict) -> str:
    """Two user-drawn crops stacked 50/50, each with gaussian blur fill.

    Uses iw/ih expressions — works on any source resolution.
    """
    ow, oh = cfg["output"]["resolution"]
    top_h = oh // 2
    bot_h = oh - top_h

    def _crop(c):
        return f"iw*{c['w']}:ih*{c['h']}:iw*{c['x']}:ih*{c['y']}"

    return (
        f"[0:v]split=2[ds_t][ds_b];"

        f"[ds_t]crop={_crop(top)},split=2[t_bg_in][t_fg_in];"
        f"[t_bg_in]scale={ow}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{top_h},boxblur=30:2[t_bg];"
        f"[t_fg_in]scale={ow}:{top_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[t_fg];"
        f"[t_bg][t_fg]overlay=(W-w)/2:(H-h)/2[top];"

        f"[ds_b]crop={_crop(bot)},split=2[b_bg_in][b_fg_in];"
        f"[b_bg_in]scale={ow}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{bot_h},boxblur=30:2[b_bg];"
        f"[b_fg_in]scale={ow}:{bot_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[b_fg];"
        f"[b_bg][b_fg]overlay=(W-w)/2:(H-h)/2[bot];"

        f"[top][bot]vstack=inputs=2[v]"
    )


def build_filter(cfg: dict, layout: str, scene: str = "scene_a",
                 crop_override: dict | None = None,
                 face_override: dict | None = None,
                 game_override: dict | None = None,
                 game_override2: dict | None = None,
                 swap: bool = False) -> str:
    if layout == "split":
        return build_split_filter(cfg, scene, crop_override=crop_override,
                                   game_override=game_override)
    elif layout == "centered":
        return build_centered_filter(cfg, scene, crop_override=crop_override,
                                     face_override=face_override)
    elif layout in ("gaussian", "webcam_only"):
        return build_webcam_only_filter(cfg, scene, crop_override=crop_override,
                                        face_override=face_override)
    elif layout == "facecam_only":
        return build_facecam_only_filter(cfg, scene, crop_override=crop_override)
    elif layout == "full_frame":
        return build_full_frame_filter(cfg)
    elif layout == "screen_only":
        return build_screen_only_filter(cfg, scene)
    elif layout == "dual_screen":
        return build_dual_screen_filter(cfg, scene,
                                        game_override=game_override,
                                        game_override2=game_override2)
    elif layout == "screen_filler":
        return build_screen_filler_filter(cfg, scene, game_override=game_override)
    elif layout == "screen_cam_split":
        return build_screen_cam_split_filter(cfg, scene,
                                             game_override=game_override,
                                             crop_override=crop_override)
    elif layout == "brainrot":
        return build_brainrot_filter(cfg, scene, crop_override=crop_override)
    elif layout == "screen_gameplay":
        return build_screen_gameplay_filter(cfg, scene, crop_override=crop_override)
    elif layout == "screen_cam":
        return build_screen_cam_filter(cfg, scene, crop_override=crop_override)
    elif layout == "cam_screen":
        return build_cam_screen_filter(cfg, scene, crop_override=crop_override)
    elif layout == "watch_party":
        return build_watch_party_filter(cfg, scene, swap=swap,
                                        crop_override=crop_override)
    elif layout == "crop_fill":
        if crop_override is None or game_override is None:
            raise ValueError("crop_fill requires both cam (purple) and gameplay (green) boxes")
        return build_crop_fill_filter(cfg, crop_override, game_override)
    elif layout == "gameplay_fill":
        if game_override is None:
            raise ValueError("gameplay_fill requires the gameplay (green) crop box")
        return build_gameplay_fill_filter(cfg, game_override)
    elif layout == "dual_crop":
        if game_override is None or game_override2 is None:
            raise ValueError("dual_crop requires both crop boxes")
        return build_dual_crop_filter(cfg, game_override, game_override2)
    raise ValueError(f"Unknown layout: {layout}")
