# Filler videos for brainrot channels

Drop looping video files into the subfolders here. Any channel config
that has `brainrot.enabled: true` will point at one of these folders
via `brainrot.filler_dir`.

## Supported formats

`.mp4`, `.mov`, `.mkv`, `.webm` — anything ffmpeg can read.

## How picking works

For each clip render on a brainrot channel:
1. Pipeline lists all video files in the channel's `filler_dir`
2. Picks one at random (seeded by clip index so re-runs are stable)
3. Probes its duration
4. Picks a random start offset inside it so the clip window fits
5. If the filler is shorter than the clip, ffmpeg loops it automatically

So you can drop in one 60-minute parkour compilation and the pipeline
will pull a different random 20-second segment for each clip. Or drop
in 20 short clips and let it pick one each time. Both work.

## Folder suggestions

- `parkour/` — Minecraft parkour, Rocket League freestyle, anything
  with consistent motion
- `satisfying/` — soap cutting, slime, kinetic sand, paint mixing,
  oddly-satisfying compilations
- `subway/` — Subway Surfers, Temple Run, fruit ninja, GTA stunt loops

Feel free to make more: `fillers/cooking`, `fillers/asmr`, etc., and
point a channel config at it.

## ⚠️  Copyright reminder

Make sure you have the right to use the footage. Using reuploaded or
copyrighted compilations can get TikTok/YT Shorts channels banned.
Safest sources:
- Streamers who explicitly release their gameplay under permissive terms
- Your own recordings
- Stock footage libraries (Pexels, Pixabay have free 4K loops)
- Creators who explicitly license for reuse
