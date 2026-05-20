"""Download a Twitch VOD and its chat log.

VOD: yt-dlp (handles Twitch fine).
Chat: Twitch Helix API doesn't serve full VOD chat history; we use
TwitchDownloaderCLI which dumps the IRC log as JSON. Install it from
https://github.com/lay295/TwitchDownloader (it's a single binary) and
make sure it's on PATH as `TwitchDownloaderCLI`.
"""
from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ChatMessage:
    offset_sec: float   # seconds from VOD start
    user: str
    text: str


def download_vod(vod_id: str, out_dir: Path) -> Path:
    """Returns path to downloaded .mp4."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{vod_id}.mp4"
    if out_path.exists():
        return out_path
    url = f"https://www.twitch.tv/videos/{vod_id}"
    subprocess.run(
        ["yt-dlp", "-f", "best", "-o", str(out_path), url],
        check=True,
    )
    return out_path


def download_chat(vod_id: str, out_dir: Path) -> list[ChatMessage]:
    """Returns chat messages with offsets in seconds from VOD start."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{vod_id}_chat.json"
    if not json_path.exists():
        subprocess.run(
            [
                "TwitchDownloaderCLI", "chatdownload",
                "--id", vod_id,
                "-o", str(json_path),
            ],
            check=True,
        )
    data = json.loads(json_path.read_text(encoding="utf-8"))
    # TwitchDownloader schema: data["comments"][i]["content_offset_seconds"],
    # ["commenter"]["display_name"], ["message"]["body"]
    msgs = []
    for c in data.get("comments", []):
        msgs.append(ChatMessage(
            offset_sec=float(c["content_offset_seconds"]),
            user=c["commenter"]["display_name"],
            text=c["message"]["body"],
        ))
    msgs.sort(key=lambda m: m.offset_sec)
    return msgs
