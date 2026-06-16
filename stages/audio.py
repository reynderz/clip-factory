"""Audio processing for renders.

Two operations:
  1. loudnorm — normalize the streamer's voice to TikTok's -14 LUFS spec.
  2. music mix — duck a background music track under the streamer's voice
     using sidechaincompress, so music dips when she talks.

Returns an ffmpeg audio filter chain (string) and any extra inputs needed.
The renderer threads them into the main filter_complex graph.
"""
from __future__ import annotations
import random
from dataclasses import dataclass
from pathlib import Path


MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


@dataclass
class AudioPlan:
    extra_inputs: list[str]      # ffmpeg -i args for music
    filter_chain: str            # filter_complex stanza producing [aout]
    output_label: str = "[aout]"


def _loudnorm_args(cfg: dict) -> str:
    ln = cfg["audio"]["loudnorm"]
    # Single-pass loudnorm. Two-pass is more accurate but doubles render time.
    return (f"loudnorm=I={ln['target_lufs']}:"
            f"TP={ln['target_tp']}:LRA={ln['target_lra']}")


def pick_music(folder: Path, seed: int | None = None) -> Path | None:
    if not folder.exists():
        return None
    files = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() in MUSIC_EXTS)
    if not files:
        return None
    if seed is None:
        return random.choice(files)
    # seed encodes: high bits = shuffle key (per VOD+variant), low 12 bits = clip position.
    # This cycles through all songs before repeating, with a different order per VOD.
    n = len(files)
    clip_pos   = seed & 0xFFF          # position in the no-repeat cycle (0-4095)
    shuffle_key = seed >> 12            # which shuffle order to use
    rng = random.Random(shuffle_key)
    shuffled = files.copy()
    rng.shuffle(shuffled)
    return shuffled[clip_pos % n]


def plan_audio(cfg: dict, music_on: bool, music_seed: int | None = None,
               music_file: Path | None = None,
               project_root: Path | None = None) -> AudioPlan:
    """Build the audio filter chain for a render.

    Voice goes [0:a] → loudnorm → either out, or sidechained with music.
    """
    ln = cfg["audio"]["loudnorm"]
    do_loudnorm = ln.get("enabled", True)
    boost_db = float(ln.get("boost_db", 0.0))
    voice_chain = "[0:a]"
    if do_loudnorm:
        voice_chain += _loudnorm_args(cfg)
        if boost_db:
            voice_chain += f",volume={boost_db}dB"
        voice_chain += ","
    elif boost_db:
        voice_chain += f"volume={boost_db}dB,"
    voice_chain += "asplit=2[voice_main][voice_sc]" if music_on else "anull[voice_main]"

    # Voice-only path
    if not music_on:
        return AudioPlan(
            extra_inputs=[],
            filter_chain=f"{voice_chain};[voice_main]anull[aout]",
            output_label="[aout]",
        )

    # Voice + music path
    music_cfg = cfg["audio"]["music"]
    if music_file is not None and Path(music_file).exists():
        track = Path(music_file)
    else:
        folder = Path(music_cfg["folder"])
        if project_root is not None and not folder.is_absolute():
            folder = project_root / folder
        track = pick_music(folder, seed=music_seed)
    if track is None:
        # No music available; gracefully fall back to voice-only
        return AudioPlan(
            extra_inputs=[],
            filter_chain=f"[0:a]{_loudnorm_args(cfg) if do_loudnorm else 'anull'}[aout]",
            output_label="[aout]",
        )

    vol_db = music_cfg.get("volume_db", -18.0)
    duck = music_cfg.get("duck_when_speaking", True)
    duck_db = music_cfg.get("duck_amount_db", -8.0)

    # Per-file start offset (skip intros); defined in config music.starts dict.
    starts = music_cfg.get("starts", {})
    start_sec = float(starts.get(track.name, 0.0))
    extra = ["-stream_loop", "-1"]
    if start_sec > 0:
        extra += ["-ss", f"{start_sec:.3f}"]
    extra += ["-i", str(track)]
    music_input_label = "[1:a]"   # music is always input #1 for audio purposes

    # Music chain: volume + (optional) sidechain compression keyed on voice
    if duck:
        # ffmpeg 8.x: makeup is linear (1-64), not dB — just omit it and let
        # the compressor's ratio handle the ducking.
        chain = (
            f"{voice_chain};"
            f"{music_input_label}volume={vol_db}dB[music_lvl];"
            f"[music_lvl][voice_sc]sidechaincompress="
            f"threshold=0.05:ratio=8:attack=20:release=400[music_ducked];"
            f"[voice_main][music_ducked]amix=inputs=2:duration=first:"
            f"dropout_transition=0[aout]"
        )
    else:
        chain = (
            f"{voice_chain};"
            f"{music_input_label}volume={vol_db}dB[music_lvl];"
            f"[voice_main][music_lvl]amix=inputs=2:duration=first:"
            f"dropout_transition=0[aout]"
        )

    return AudioPlan(extra_inputs=extra, filter_chain=chain,
                     output_label="[aout]")
