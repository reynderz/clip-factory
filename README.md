# Clip Factory

> New to this / geen programmeerervaring? Read **[STARTHIER.md](STARTHIER.md)**
> first — a full Windows install + usage guide in Dutch and English for
> non-technical users.

Twitch-VOD → many-variant TikTok clips. One review pass, every approved clip
gets rendered through every applicable variant (centered, blurred, brainrot
with parkour/minecraft, with/without chat overlay, with/without music).
Scene-aware: three scene types are detected and handled differently.

## Scenes

| ID | OBS scene | Description | Variants |
|----|-----------|-------------|----------|
| `scene_a` | Gaming | Chat sidebar left · game center · webcam bottom-right | (extend as needed) |
| `scene_b` | Just Chatting | Full-frame webcam · chat sidebar right | centered, gaussian, brainrot (parkour/subway/minecraft) |
| `scene_c` | Watch Party | Chat sidebar left · anime/video center · webcam top-right | watch_party (normal + swapped) |

Watch-party variants (`watch_party_*`):
- `watch_party_chat_nomusic` — webcam top (40 %), video bottom (60 %), no music
- `watch_party_nochat_nomusic` — same, no chat overlay
- `watch_party_swapped_chat_nomusic` — video top (60 %), webcam bottom (40 %), no music
- `watch_party_swapped_nochat_nomusic` — same, no chat overlay

## Pipeline overview

1. Download VOD + chat log (yt-dlp + TwitchDownloaderCLI)
2. Detect moments via chat velocity + audio peaks + chat-reading
3. Coarse full-VOD transcription (cached) for chat-reading detection
4. Interactive review CLI — you approve clips and pick chat messages
5. Per-clip: detect scene (gaming vs chatting), transcribe, apply word-level karaoke subs
6. Render every applicable variant from `configs/_global.yaml > variants`

## System deps

- ffmpeg + ffprobe
- yt-dlp
- TwitchDownloaderCLI on PATH (https://github.com/lay295/TwitchDownloader/releases)
- Komika Axis font installed system-wide (`KOMIKAX_.ttf`)

### Windows setup

```powershell
winget install Git.Git Gyan.FFmpeg Python.Python.3.12
```

- **yt-dlp**: `pip install yt-dlp` (already in `requirements.txt`, but you can
  also `winget install yt-dlp.yt-dlp` if you want it globally on PATH too).
- **TwitchDownloaderCLI**: download the `win-x64` build from
  https://github.com/lay295/TwitchDownloader/releases, unzip it, and put the
  folder containing `TwitchDownloaderCLI.exe` on your PATH (System Properties
  → Environment Variables → Path).
- **Komika Axis font**: put `KOMIKAX_.ttf` in
  `%LOCALAPPDATA%\Microsoft\Windows\Fonts\` **and** right-click it →
  "Install" (or "Install for all users") so Windows registers the family
  name — ffmpeg's subtitle renderer (libass) looks it up by name, not by
  file path, so just dropping the file in a folder isn't enough.
- **Encoder**: `configs/_global.yaml` defaults to `libx264` (CPU, works
  everywhere). If you have an NVIDIA/Intel/AMD GPU you can switch
  `output.encoder` to `h264_nvenc`, `h264_qsv`, or `h264_amf` for faster
  renders — run `ffmpeg -encoders | findstr h264` to see what your ffmpeg
  build supports.
- Run everything from **PowerShell**, not `cmd.exe`.

## Python

macOS / Linux:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by execution policy, run PowerShell as your
user and allow local scripts once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Configure

All settings live in `configs/_global.yaml`. The two sections you'll touch most:

**`layout.scenes`** — webcam and gameplay coordinates per OBS scene. Adjust
fractions after first render if framing looks off.

**`variants`** — list of render recipes. Each clip × each applicable variant
= one output file. Comment out lines to skip variants. Copy entries to add
your own combinations (e.g. brainrot with subway surfers).

List configured variants:
```bash
python pipeline.py variants
```

## Calibrate scene classifier (once)

A vs B only:
```bash
python calibrate.py work/<vod_id>/<vod_id>.mp4 \
    --scene-a 340 920 1500 \
    --scene-b 60 180 2400
```

All three scenes (including watch party):
```bash
python calibrate.py work/<vod_id>/<vod_id>.mp4 \
    --scene-a 340 920 1500 \
    --scene-b 60 180 2400 \
    --scene-c 600 1200 3000
```

Paste suggested values into `_global.yaml`:
- `scene.scene_a_chat_edge_min` — separates B (low left) from A/C (high left)
- `scene.scene_c_right_edge_max` — separates C (face in right box, low) from A (game, high)

## Daily workflow

```bash
python pipeline.py detect <vod_id>     # download, transcribe, detect, review
python pipeline.py render <vod_id>     # render all variants for all approved clips
```

Or only specific variants:
```bash
python pipeline.py render <vod_id> --only-variants centered_chat_music,parkour_chat_music
```

Output filenames:
```
out/<vod_id>/clip_001__centered_chat_nomusic.mp4
out/<vod_id>/clip_001__parkour_nochat_music.mp4
out/<vod_id>/clip_001__gaussian_chat_nomusic.mp4
out/<vod_id>/clip_002__gaming_split_chat.mp4
...
```

The variant name in the filename tells the mod exactly what they're getting.

## Fillers and music

Drop content into:
- `fillers/parkour/` — Minecraft parkour or anything fast-motion
- `fillers/minecraft/` — Minecraft gameplay loops
- `fillers/satisfying/` — soap cutting / slime / oddly satisfying
- `music/` — royalty-free background music tracks

See each folder's README for sourcing tips and copyright caveats.

## Realistic output volumes

For a typical VOD with 10 approved clips (mix of Scene A + Scene B):
- 6 Scene B clips × ~10 applicable variants ≈ 60 outputs
- 4 Scene A clips × 2 gaming variants ≈ 8 outputs
- Total: ~68 files, ~50-90 minutes to render on M3 Pro with VideoToolbox

## Audio

- **Loudnorm** is on by default, normalizing every output to -14 LUFS
  (TikTok spec). Quiet streams get boosted automatically — no manual gain.
- **Music variants** mix a randomly-chosen track from `music/` under the
  streamer's voice with sidechain compression (music dips when she talks).
  See `music/README.md` for the TikTok-specific copyright caveat.

## Files

```
clip_factory/
├── pipeline.py             # detect / render / auto / variants commands
├── review.py               # interactive approval + chat picker
├── calibrate.py            # scene threshold calibration
├── configs/
│   └── _global.yaml        # one config to rule them all
├── fillers/
│   ├── parkour/
│   ├── minecraft/
│   └── satisfying/
├── music/
└── stages/
    ├── cfg.py
    ├── fetch.py
    ├── detect_moments.py
    ├── detect_chat_reading.py
    ├── transcribe_vod.py
    ├── pick_message.py
    ├── classify_scene.py
    ├── crop.py
    ├── transcribe.py
    ├── subtitle.py
    ├── chat_overlay.py
    ├── filler.py
    ├── audio.py            # loudnorm + music mix + ducking
    └── render.py
```
