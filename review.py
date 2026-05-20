"""Interactive review loop with chat-message picker.

For each candidate:
  - Generate a preview GIF around the peak.
  - If the candidate looks like a chat-read (reason contains 'chat_read')
    OR the user opts in, show the top-5 candidate messages and let them
    pick one (or skip overlay entirely).
  - Approve (y) or reject (n); q quits the review loop.

Output: approved.json with each approved candidate possibly carrying a
`chat_message` field ({user, text, offset_sec}).
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

from stages.pick_message import rank_messages
from stages.fetch import ChatMessage


def make_preview(vod: Path, peak_sec: float, out: Path,
                 width: int = 360, duration: float = 3.0) -> Path:
    start = max(0.0, peak_sec - duration / 2)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}",
        "-i", str(vod),
        "-t", f"{duration:.2f}",
        "-vf", f"fps=10,scale={width}:-1:flags=lanczos",
        "-loop", "0",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _prompt_message(peak_sec: float, transcript_path: Path,
                    msgs: list[ChatMessage], cfg: dict) -> dict | None:
    """Show top-5 chat candidates, let user pick or skip."""
    candidates = rank_messages(peak_sec, transcript_path, msgs, cfg, top_k=5)
    if not candidates:
        print("  (no chat messages in window)")
        return None
    print("  chat message candidates:")
    for idx, mc in enumerate(candidates, 1):
        dt = mc.offset_sec - peak_sec
        text = mc.text if len(mc.text) <= 70 else mc.text[:67] + "..."
        print(f"    [{idx}] score={mc.score:5.1f}  dt={dt:+5.1f}s  "
              f"{mc.user}: {text}")
    print("    [0] no overlay")
    while True:
        ans = input("  pick [0-5]: ").strip()
        if ans in {"0", "1", "2", "3", "4", "5"}:
            break
    if ans == "0":
        return None
    mc = candidates[int(ans) - 1]
    return {"user": mc.user, "text": mc.text, "offset_sec": mc.offset_sec}


def review(candidates: list[dict], vod: Path,
           preview_dir: Path, out_path: Path,
           cfg: dict | None = None,
           transcript_path: Path | None = None,
           msgs: list[ChatMessage] | None = None) -> list[dict]:
    preview_dir.mkdir(parents=True, exist_ok=True)
    approved: list[dict] = []
    total = len(candidates)
    have_chat = (cfg is not None and transcript_path is not None
                 and msgs is not None and transcript_path.exists())

    for i, c in enumerate(candidates, 1):
        gif = preview_dir / f"cand_{i:03d}.gif"
        try:
            make_preview(vod, c["peak_sec"], gif)
        except subprocess.CalledProcessError:
            print(f"[{i}/{total}] preview failed, skipping"); continue

        print(f"\n[{i}/{total}] t={c['peak_sec']:.1f}s  "
              f"score={c['score']:.2f}  {c['reasons']}")
        print(f"  preview: {gif}")

        while True:
            ans = input("  keep? [y/n/c=pick chat msg/q=quit]: ").strip().lower()
            if ans in ("y", "n", "c", "q"):
                break
        if ans == "q":
            break
        if ans == "n":
            continue

        # Auto-offer chat picker if this looks like a chat-read
        reasons_joined = " ".join(c.get("reasons", []))
        is_chat_read = "chat_read" in reasons_joined

        if ans == "c" or (ans == "y" and is_chat_read and have_chat):
            if have_chat:
                msg = _prompt_message(c["peak_sec"], transcript_path,
                                      msgs, cfg)
                if msg:
                    c["chat_message"] = msg
            else:
                print("  (chat picker unavailable — transcript missing)")

        approved.append(c)

    out_path.write_text(json.dumps(approved, indent=2))
    print(f"\nApproved {len(approved)} / {total}. Saved to {out_path}")
    return approved
