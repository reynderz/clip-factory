# Background music

Drop royalty-free or licensed audio files here:
`.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.flac` are all supported.

For each clip rendered with `music: on`, the pipeline picks one track at
random and mixes it under the streamer's voice. If `duck_when_speaking`
is true (default) the music auto-dips when she's talking via sidechain
compression — so dialogue stays clear.

## ⚠️  Copyright reality check

If you're targeting **TikTok specifically**, you almost certainly want to
leave music OFF here and add TikTok's native sounds inside the app after
upload. Their algorithm boosts videos using trending sounds, and
copyrighted songs baked into the video will get the clip muted or the
account flagged.

Safe sources:
- YouTube Audio Library (free, royalty-free, no attribution needed)
- Pixabay Music (free, broad license)
- Epidemic Sound (paid subscription, very safe for monetized accounts)
- Artists who explicitly allow non-commercial reuse (some lo-fi creators)

## Volume tuning

Adjust in `configs/_global.yaml` under `audio.music`:
- `volume_db: -18.0` is a sensible default (music sits well under speech)
- `duck_amount_db: -8.0` is how much extra it dips during speech
- `duck_when_speaking: false` if you want flat-mixed music instead
