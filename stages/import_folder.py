"""Import a folder of premade/pre-cut clips as the source for a project,
instead of downloading+detecting moments in a full Twitch VOD.

Each file in the folder becomes one candidate spanning its whole duration.
All clips are normalised (scaled/padded to a common resolution+fps, audio
resampled) and concatenated into a single work/<vod_id>/<vod_id>.mp4 so the
rest of the pipeline (scene classification, transcription, rendering) can
treat it exactly like a downloaded VOD — just seeking into different offsets.
"""
from __future__ import annotations
import json
import subprocess
from fractions import Fraction
from pathlib import Path

CLIP_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".avi", ".flv"}


def list_clip_files(folder: Path) -> list[Path]:
    """Return video files directly inside `folder`, sorted by filename."""
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in CLIP_EXTS
    )


def _probe(path: Path) -> dict:
    """Return {duration, width, height, fps, has_audio} for a clip."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,width,height,r_frame_rate",
         str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    duration = float(data["format"]["duration"])
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None:
        raise ValueError(f"No video stream in {path}")
    fps = float(Fraction(video["r_frame_rate"]))
    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "has_audio": has_audio,
    }


def concat_clips(clips: list[Path], out_path: Path) -> list[float]:
    """Normalise + concatenate `clips` into `out_path`. Returns each clip's
    duration (seconds), in the same order, for building candidate offsets."""
    infos = [_probe(c) for c in clips]
    durations = [i["duration"] for i in infos]

    # Normalise to the first clip's resolution/fps — premade clips from the
    # same source (e.g. Twitch clips of one stream) are almost always uniform.
    width, height, fps = infos[0]["width"], infos[0]["height"], infos[0]["fps"]

    inputs: list[str] = []
    filter_parts: list[str] = []
    next_idx = 0
    for i, (clip, info) in enumerate(zip(clips, infos)):
        inputs += ["-i", str(clip)]
        vidx = next_idx
        next_idx += 1
        if info["has_audio"]:
            aidx = vidx
        else:
            inputs += ["-f", "lavfi", "-t", f"{info['duration']:.3f}",
                      "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            aidx = next_idx
            next_idx += 1

        filter_parts.append(
            f"[{vidx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps}[v{i}]"
        )
        filter_parts.append(
            f"[{aidx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
        )

    concat_refs = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    filter_parts.append(f"{concat_refs}concat=n={len(clips)}:v=1:a=1[outv][outa]")

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return durations


def build_candidates(clips: list[Path], durations: list[float]) -> list[dict]:
    """One candidate per clip, spanning its full duration in the
    concatenated timeline."""
    cands = []
    cum = 0.0
    for clip, dur in zip(clips, durations):
        start, end = cum, cum + dur
        cands.append({
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "peak_sec": round(start + dur / 2, 3),
            "score": 1.0,
            "reasons": [f"premade: {clip.name}"],
        })
        cum = end
    return cands


def import_folder(folder: Path, out_vod: Path) -> list[dict]:
    """Full import: scan folder, concat into out_vod, return candidates."""
    clips = list_clip_files(folder)
    if not clips:
        raise FileNotFoundError(f"No video files found in {folder}")
    print(f"  found {len(clips)} clip(s):")
    for c in clips:
        print(f"    - {c.name}")
    durations = concat_clips(clips, out_vod)
    return build_candidates(clips, durations)
