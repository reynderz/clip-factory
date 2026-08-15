"""Web-based review GUI for the Twitch VOD clip factory.

Usage:
    python gui.py <vod_id>

Opens a browser at http://localhost:5001 automatically.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import NotFound

ROOT = Path(__file__).parent
WORK = ROOT / "work"

# Hard cap on how much audio a single /api/transcribe call will feed to
# whisper. Some detected candidates span tens of minutes (merged moments),
# which makes transcription effectively hang. Clip to a window around the
# peak instead.
MAX_TRANSCRIBE_SEC = 90.0

app = Flask(__name__)
app.config["VOD_ID"] = None

# ── subprocess state ──────────────────────────────────────────────
_render_proc: subprocess.Popen | None = None
_render_state = "idle"   # idle | running | done | error
_render_exit_code: int | None = None
_render_lock = threading.Lock()
_render_log: deque = deque(maxlen=200)

_detect_proc: subprocess.Popen | None = None
_detect_state = "idle"
_detect_exit_code: int | None = None
_detect_lock = threading.Lock()
_detect_log: deque = deque(maxlen=200)

_import_proc: subprocess.Popen | None = None
_import_state = "idle"
_import_exit_code: int | None = None
_import_lock = threading.Lock()
_import_log: deque = deque(maxlen=200)


# ── helpers ───────────────────────────────────────────────────────
def _work() -> Path:
    return WORK / app.config["VOD_ID"]


def _candidates_path() -> Path:
    return _work() / "candidates.json"


def _approved_path() -> Path:
    return _work() / "approved.json"


def _chat_path() -> Path:
    vid = app.config["VOD_ID"]
    return _work() / f"{vid}_chat.json"


def _load_candidates() -> list[dict]:
    p = _candidates_path()
    if not p.exists():
        return []
    return json.loads(p.read_text())


def _load_approved() -> list[dict]:
    p = _approved_path()
    if not p.exists():
        return []
    return json.loads(p.read_text())


def _save_approved(clips: list[dict]) -> None:
    _approved_path().write_text(json.dumps(clips, indent=2))


def _load_chat_msgs() -> list[dict]:
    """Return list of {user, text, offset_sec} parsed from TwitchDownloaderCLI JSON."""
    p = _chat_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        msgs = []
        for c in data.get("comments", []):
            msgs.append({
                "offset_sec": float(c["content_offset_seconds"]),
                "user": c["commenter"]["display_name"],
                "text": c["message"]["body"],
            })
        msgs.sort(key=lambda m: m["offset_sec"])
        return msgs
    except Exception as e:
        app.logger.warning("Failed to parse chat JSON: %s", e)
        return []


def _has_transcript() -> bool:
    return (_work() / "vod_transcript.json").exists()


def _part_offset() -> float:
    """VOD time offset for split parts (e.g. part2 starts at 11490 s in the original VOD).
    Reads from a sidecar .offset file next to the video in vods/."""
    vod_id = app.config["VOD_ID"]
    vods_dir = ROOT.parent / "vods"
    f = vods_dir / f"{vod_id}.offset"
    if f.exists():
        try:
            return float(f.read_text().strip())
        except Exception:
            pass
    return 0.0


def _ffmpeg_env() -> dict:
    env = os.environ.copy()
    ffmpeg_bin = "/opt/homebrew/opt/ffmpeg-full/bin"
    path = env.get("PATH", "")
    if ffmpeg_bin not in path:
        env["PATH"] = ffmpeg_bin + ":" + path
    return env


def _stream_proc(proc: subprocess.Popen, log_buf: deque,
                 state_ref: list, exit_ref: list, lock: threading.Lock) -> None:
    """Background thread: drain proc stdout into log_buf, then update state."""
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip() if isinstance(raw, bytes) else raw.rstrip()
        if line:
            with lock:
                log_buf.append(line)
    proc.wait()
    with lock:
        exit_ref[0] = proc.returncode
        state_ref[0] = "done" if proc.returncode == 0 else "error"


# ── routes ────────────────────────────────────────────────────────
@app.after_request
def _no_cache(response):
    """Prevent the browser from caching pages or API responses."""
    if not request.path.startswith("/video/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    sys.path.insert(0, str(ROOT))
    try:
        from stages.cfg import load_global
        cfg = load_global()
        scenes = cfg.get("layout", {}).get("scenes", {})
    except Exception:
        scenes = {}
    with _detect_lock:
        detect_state = _detect_state
        detect_log_lines = list(_detect_log)
    return render_template(
        "gui.html",
        vod_id=app.config["VOD_ID"],
        scenes=scenes,
        detect_state=detect_state,
        detect_log_lines=detect_log_lines,
    )


@app.route("/video/<vod_id>")
def serve_video(vod_id: str):
    vod_path = (WORK / vod_id / f"{vod_id}.mp4").resolve()
    if not vod_path.exists():
        raise NotFound(f"VOD not found: {vod_path}")
    return send_file(str(vod_path), mimetype="video/mp4", conditional=True)


@app.route("/font/KOMIKAX_.ttf")
def serve_font():
    font_path = Path.home() / "Library" / "Fonts" / "KOMIKAX_.ttf"
    if not font_path.exists():
        raise NotFound("Font not found")
    return send_file(str(font_path), mimetype="font/truetype")


@app.route("/api/ping")
def api_ping():
    """Diagnostic: called by browser img probes so we can see JS execution progress."""
    step = request.args.get("step", "?")
    err  = request.args.get("e", "")
    if err:
        app.logger.warning("BROWSER JS ERROR at %s: %s", step, err)
    else:
        app.logger.info("BROWSER PING step=%s", step)
    resp = app.make_response(b'')
    resp.content_type = "image/gif"
    return resp


@app.route("/api/state")
def api_state():
    return jsonify({
        "vod_id": app.config["VOD_ID"],
        "candidates": _load_candidates(),
        "approved": _load_approved(),
        "has_transcript": _has_transcript(),
    })


@app.route("/api/messages")
def api_messages():
    chat_msgs = _load_chat_msgs()
    offset = _part_offset()  # 0.0 for non-split VODs

    # Clip-window mode: return all messages in [start_sec, end_sec]
    start_raw = request.args.get("start_sec")
    end_raw   = request.args.get("end_sec")
    if start_raw is not None and end_raw is not None:
        try:
            start_sec = float(start_raw)
            end_sec   = float(end_raw)
        except ValueError:
            return jsonify({"error": "bad start_sec or end_sec"}), 400
        # Convert video-relative window to absolute VOD timestamps for filtering
        vod_start = start_sec + offset
        vod_end   = end_sec   + offset
        results = [
            {"user": m["user"], "text": m["text"],
             "offset_sec": m["offset_sec"] - offset, "score": 0.0}
            for m in chat_msgs
            if vod_start <= m["offset_sec"] <= vod_end
        ]
        return jsonify(results)

    # Peak-based mode (original behaviour)
    try:
        peak_sec = float(request.args.get("peak_sec", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "bad peak_sec"}), 400

    peak_vod = peak_sec + offset  # absolute VOD time for chat lookup
    transcript_path = _work() / "vod_transcript.json"
    results = []

    if _has_transcript() and chat_msgs:
        try:
            sys.path.insert(0, str(ROOT))
            from stages.pick_message import rank_messages
            from stages.fetch import ChatMessage
            from stages.cfg import load_global

            cfg = load_global()
            fetch_msgs = [
                ChatMessage(offset_sec=m["offset_sec"],
                            user=m["user"], text=m["text"])
                for m in chat_msgs
            ]
            ranked = rank_messages(peak_vod, transcript_path, fetch_msgs, cfg, top_k=5)
            results = [
                {"user": r.user, "text": r.text,
                 "offset_sec": r.offset_sec - offset, "score": round(r.score, 1)}
                for r in ranked
            ]
        except Exception as e:
            app.logger.warning("rank_messages failed (%s), falling back", e)

    if not results:
        window_sec = 30.0
        for m in chat_msgs:
            if abs(m["offset_sec"] - peak_vod) <= window_sec:
                results.append({
                    "user": m["user"],
                    "text": m["text"],
                    "offset_sec": m["offset_sec"] - offset,
                    "score": 0.0,
                })
        results = results[:5]

    return jsonify(results)


@app.route("/api/approve", methods=["POST"])
def api_approve():
    clip = request.get_json(force=True)
    if clip is None or "peak_sec" not in clip:
        return jsonify({"error": "missing peak_sec"}), 400

    approved = _load_approved()
    # Upsert: replace existing entry with same peak_sec
    peak = float(clip["peak_sec"])
    approved = [c for c in approved if abs(float(c["peak_sec"]) - peak) > 0.01]
    approved.append(clip)
    approved.sort(key=lambda c: c["start_sec"])
    _save_approved(approved)
    return jsonify({"ok": True, "count": len(approved)})


@app.route("/api/crops/apply_all", methods=["POST"])
def api_crops_apply_all():
    """Overwrite cam_crop/game_crop/game_crop2 on every approved clip at once."""
    body = request.get_json(force=True) or {}
    crops = {k: body[k] for k in ("cam_crop", "game_crop", "game_crop2") if body.get(k)}
    approved = _load_approved()
    for clip in approved:
        clip.update(crops)
    _save_approved(approved)
    return jsonify({"ok": True, "updated": len(approved)})


@app.route("/api/candidates/add", methods=["POST"])
def api_candidates_add():
    body = request.get_json(force=True) or {}
    try:
        start_sec = float(body["start_sec"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "missing or invalid start_sec"}), 400

    duration = 60.0
    end_sec  = start_sec + duration
    peak_sec = start_sec + duration / 2

    candidate = {
        "start_sec": round(start_sec, 3),
        "end_sec":   round(end_sec,   3),
        "peak_sec":  round(peak_sec,  3),
        "score":     1.0,
        "reasons":   ["manual"],
    }

    p = _candidates_path()
    candidates = json.loads(p.read_text()) if p.exists() else []
    candidates.append(candidate)
    candidates.sort(key=lambda c: c["start_sec"])
    p.write_text(json.dumps(candidates, indent=2))

    return jsonify({"ok": True, "candidate": candidate})


@app.route("/api/unapprove", methods=["POST"])
def api_unapprove():
    body = request.get_json(force=True)
    if body is None or "peak_sec" not in body:
        return jsonify({"error": "missing peak_sec"}), 400

    peak = float(body["peak_sec"])
    approved = _load_approved()
    before = len(approved)
    approved = [c for c in approved if abs(float(c["peak_sec"]) - peak) > 0.01]
    _save_approved(approved)
    return jsonify({"ok": True, "removed": before - len(approved)})


@app.route("/api/render/start", methods=["POST"])
def api_render_start():
    global _render_proc, _render_state, _render_exit_code

    with _render_lock:
        if _render_state == "running":
            return jsonify({"ok": False, "error": "already running"})

        body = request.get_json(force=True) or {}
        cmd = [sys.executable, "-u", str(ROOT / "pipeline.py"), "render",
               app.config["VOD_ID"]]
        only = body.get("only_variants", "").strip()
        if only:
            cmd += ["--only-variants", only]
            
        only_clip = body.get("only_clip")
        if only_clip is not None:
            cmd += ["--only-clip", str(only_clip)]

        subs_y_frac = body.get("subs_y_frac")
        if subs_y_frac is not None:
            cmd += ["--subs-y-frac", str(subs_y_frac)]

        subs_font = body.get("subs_font", "").strip()
        if subs_font:
            cmd += ["--subs-font", subs_font]

        subs_font_size = body.get("subs_font_size")
        if subs_font_size is not None:
            cmd += ["--subs-font-size", str(int(subs_font_size))]

        subs_style = body.get("subs_style", "").strip()
        if subs_style:
            cmd += ["--subs-style", subs_style]

        if body.get("emoji_theme"):
            cmd += ["--emoji-theme"]

        if body.get("force_chat"):
            cmd += ["--force-chat"]

        # Position applies to any clip that shows chat (via force_chat OR a
        # per-clip selected message), so it must NOT be gated on force_chat —
        # otherwise picking "Middle" does nothing unless force_chat is also on.
        chat_y = body.get("chat_y_frac")
        if chat_y is not None:
            cmd += ["--chat-y-frac", str(chat_y)]

        music_file = body.get("music_file", "").strip()
        if music_file:
            cmd += ["--music-file", music_file]

        _render_state = "running"
        _render_exit_code = None
        _render_log.clear()
        _render_proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=_ffmpeg_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def _watch_render():
        global _render_state, _render_exit_code
        for raw in _render_proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                with _render_lock:
                    _render_log.append(line)
        _render_proc.wait()
        with _render_lock:
            _render_exit_code = _render_proc.returncode
            _render_state = "done" if _render_proc.returncode == 0 else "error"

    threading.Thread(target=_watch_render, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/render/status")
def api_render_status():
    with _render_lock:
        resp = {"state": _render_state}
        if _render_exit_code is not None:
            resp["exit_code"] = _render_exit_code
    return jsonify(resp)


@app.route("/api/detect/start", methods=["POST"])
def api_detect_start():
    global _detect_proc, _detect_state, _detect_exit_code

    with _detect_lock:
        if _detect_state == "running":
            return jsonify({"ok": False, "error": "already running"})

        body = request.get_json(force=True) or {}
        cmd = [sys.executable, "-u", str(ROOT / "pipeline.py"), "detect_only",
               app.config["VOD_ID"]]
        if body.get("max_sec"):
            cmd += ["--max-sec", str(float(body["max_sec"]))]
        _detect_state = "running"
        _detect_exit_code = None
        _detect_log.clear()
        _detect_proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=_ffmpeg_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def _watch_detect():
        global _detect_state, _detect_exit_code
        for raw in _detect_proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                with _detect_lock:
                    _detect_log.append(line)
        _detect_proc.wait()
        with _detect_lock:
            _detect_exit_code = _detect_proc.returncode
            _detect_state = "done" if _detect_proc.returncode == 0 else "error"

    threading.Thread(target=_watch_detect, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/detect/status")
def api_detect_status():
    with _detect_lock:
        resp = {"state": _detect_state}
        if _detect_exit_code is not None:
            resp["exit_code"] = _detect_exit_code
    return jsonify(resp)


@app.route("/api/import/start", methods=["POST"])
def api_import_start():
    """Import a folder of premade clips as a new project — replaces the
    VOD-download + moment-detection steps entirely."""
    global _import_proc, _import_state, _import_exit_code

    with _import_lock:
        if _import_state == "running":
            return jsonify({"ok": False, "error": "already running"})

        body = request.get_json(force=True) or {}
        vod_id = body.get("vod_id", "").strip()
        clip_dir = body.get("dir", "").strip()
        if not vod_id:
            return jsonify({"ok": False, "error": "missing vod_id"}), 400
        if not clip_dir:
            return jsonify({"ok": False, "error": "missing dir"}), 400

        folder = Path(clip_dir).expanduser()
        if not folder.is_dir():
            return jsonify({"ok": False, "error": f"not a folder: {folder}"}), 400

        (WORK / vod_id).mkdir(parents=True, exist_ok=True)
        app.config["VOD_ID"] = vod_id

        cmd = [sys.executable, "-u", str(ROOT / "pipeline.py"), "import_clips",
               vod_id, "--dir", str(folder)]
        _import_state = "running"
        _import_exit_code = None
        _import_log.clear()
        _import_proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=_ffmpeg_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def _watch_import():
        global _import_state, _import_exit_code
        for raw in _import_proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                with _import_lock:
                    _import_log.append(line)
        _import_proc.wait()
        with _import_lock:
            _import_exit_code = _import_proc.returncode
            _import_state = "done" if _import_proc.returncode == 0 else "error"

    threading.Thread(target=_watch_import, daemon=True).start()
    return jsonify({"ok": True, "vod_id": vod_id})


@app.route("/api/import/status")
def api_import_status():
    with _import_lock:
        resp = {"state": _import_state}
        if _import_exit_code is not None:
            resp["exit_code"] = _import_exit_code
    return jsonify(resp)


@app.route("/api/clip_folders/list")
def api_clip_folders_list():
    """List subfolders under vods/ that might contain premade clips."""
    vods_dir = ROOT.parent / "vods"
    if not vods_dir.is_dir():
        return jsonify([])
    folders = sorted(
        (p for p in vods_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    result = []
    for p in folders:
        count = sum(1 for f in p.iterdir()
                    if f.is_file() and f.suffix.lower() in
                    {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".avi", ".flv"})
        if count:
            result.append({"name": p.name, "path": str(p), "clip_count": count})
    return jsonify(result)


_transcript_cache: dict = {}   # f"{vod_id}_{peak_sec}" -> list[dict]


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    body = request.get_json(force=True) or {}
    vod_id = app.config["VOD_ID"]
    start_sec = float(body.get("start_sec", 0))
    end_sec   = float(body.get("end_sec",   start_sec + 30))
    peak_sec  = float(body.get("peak_sec",  start_sec))

    # Detected candidates can span tens of minutes after merging adjacent
    # moments. Clamp to a window around the peak so transcription doesn't
    # effectively hang on a huge clip.
    if end_sec - start_sec > MAX_TRANSCRIBE_SEC:
        half = MAX_TRANSCRIBE_SEC / 2
        start_sec = max(start_sec, peak_sec - half)
        end_sec = min(end_sec, peak_sec + half)

    print(f"[transcribe] peak={peak_sec:.2f} start={start_sec:.2f} end={end_sec:.2f}", flush=True)
    cache_key = f"{vod_id}_{start_sec:.3f}_{end_sec:.3f}"
    if cache_key in _transcript_cache:
        print(f"[transcribe] cache HIT for peak={peak_sec:.2f}", flush=True)
        return jsonify({"words": _transcript_cache[cache_key], "cached": True})

    vod_path = (_work() / f"{vod_id}.mp4").resolve()
    if not vod_path.exists():
        return jsonify({"error": "VOD file not found"}), 404

    # Twitch VODs downloaded with yt-dlp keep the original HLS broadcast PTS,
    # so the container start_time is non-zero. The browser player normalises
    # this away (its t=0 == file PTS start_time), so every ffmpeg seek must
    # add the PTS offset to land at the right place.
    sys.path.insert(0, str(ROOT))
    from stages.render import vod_pts_offset
    pts_off = vod_pts_offset(vod_path)

    duration = max(1.0, end_sec - start_sec)
    seek_pos = start_sec + pts_off

    # Use dual-seek for sample-accurate audio extraction:
    # 1. Fast input seek to ~10 s before the target (keyframe-aligned, cheap)
    # 2. Precise output seek to trim the remaining gap
    # This avoids the keyframe-alignment offset that makes whisper timestamps
    # start a second or two before the actual clip start.
    _margin = 10.0
    pre_seek  = max(0.0, seek_pos - _margin)
    fine_seek = seek_pos - pre_seek

    tmp = Path(tempfile.mktemp(suffix=".wav"))
    try:
        subprocess.run(
            ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-y",
             "-ss", f"{pre_seek:.3f}", "-i", str(vod_path),
             "-ss", f"{fine_seek:.3f}",
             "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
             str(tmp)],
            check=True, capture_output=True,
        )
        sys.path.insert(0, str(ROOT))
        from stages.transcribe import transcribe as do_transcribe
        words = do_transcribe(tmp)
        word_dicts = [
            {"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)}
            for w in words
        ]
        _transcript_cache[cache_key] = word_dicts
        return jsonify({"words": word_dicts, "cached": False})
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:300] if e.stderr else ""
        return jsonify({"error": f"ffmpeg: {stderr}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        tmp.unlink(missing_ok=True)


@app.route("/api/sfx/list")
def api_sfx_list():
    sfx_dir = ROOT / "sfx"
    files = sorted(p.name for p in sfx_dir.glob("*.mp3")) + \
            sorted(p.name for p in sfx_dir.glob("*.wav"))
    return jsonify({"files": files})


@app.route("/api/log")
def api_log():
    kind = request.args.get("type", "detect")
    after = int(request.args.get("after", 0))
    bufs = {"detect": (_detect_log, _detect_lock),
            "render": (_render_log, _render_lock),
            "import": (_import_log, _import_lock)}
    buf, lock = bufs.get(kind, (_detect_log, _detect_lock))
    with lock:
        lines = list(buf)
    # return only lines after the given offset (for incremental polling)
    return jsonify({"lines": lines[after:], "total": len(lines)})


@app.route("/api/music")
def api_music():
    """List available music files."""
    from stages.cfg import load_global
    try:
        cfg = load_global()
        folder = Path(cfg["audio"]["music"]["folder"])
        if not folder.is_absolute():
            folder = ROOT / folder
    except Exception:
        folder = ROOT / "music"
    exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    if not folder.exists():
        return jsonify([])
    files = sorted(p.name for p in folder.iterdir() if p.suffix.lower() in exts)
    return jsonify(files)


@app.route("/api/variants")
def api_variants():
    try:
        sys.path.insert(0, str(ROOT))
        from stages.cfg import load_global, load_variants
        cfg = load_global()
        variants = load_variants(cfg)
        return jsonify(variants)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vods/list")
def api_vods_list():
    vods_dir = ROOT.parent / "vods"
    if not vods_dir.is_dir():
        return jsonify([])
    files = sorted(
        vods_dir.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return jsonify([
        {"name": p.name, "path": str(p), "size_gb": round(p.stat().st_size / 1e9, 2)}
        for p in files
    ])


@app.route("/api/vod/set", methods=["POST"])
def api_vod_set():
    body = request.get_json(force=True) or {}
    vod_id = body.get("vod_id", "").strip()
    file_path = body.get("file_path", "").strip()

    if not vod_id and not file_path:
        return jsonify({"error": "provide vod_id or file_path"}), 400

    # Derive vod_id from filename when only file_path given
    if file_path and not vod_id:
        vod_id = Path(file_path).stem.split("-")[0]

    if not vod_id:
        return jsonify({"error": "could not determine vod_id"}), 400

    work = WORK / vod_id
    work.mkdir(parents=True, exist_ok=True)

    if file_path:
        fp = Path(file_path).expanduser().resolve()
        if not fp.exists():
            return jsonify({"error": f"File not found: {file_path}"}), 400
        link = work / f"{vod_id}.mp4"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(fp)

    app.config["VOD_ID"] = vod_id
    return jsonify({"ok": True, "vod_id": vod_id})


# ── entry point ───────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python gui.py <vod_id>")
        sys.exit(1)

    vod_id = sys.argv[1]
    app.config["VOD_ID"] = vod_id

    # Create work directory if it doesn't exist yet
    work_dir = WORK / vod_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # Auto-symlink the downloaded VOD if it isn't already in the work dir
    link = work_dir / f"{vod_id}.mp4"
    if not link.exists() and not link.is_symlink():
        vods_dir = ROOT.parent / "vods"
        matches = sorted(vods_dir.glob(f"{vod_id}*.mp4")) if vods_dir.is_dir() else []
        if matches:
            link.symlink_to(matches[0].resolve())
            print(f"Linked VOD: {matches[0]}")

    url = "http://localhost:5002"
    # Open browser slightly after Flask starts
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"GUI starting at {url}  (vod_id={vod_id})")
    app.run(host="0.0.0.0", port=5002, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
