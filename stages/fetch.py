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
    """Returns path to downloaded .mp4.

    If the file already exists (including symlinks to local files), it is
    returned immediately without attempting a Twitch download.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{vod_id}.mp4"
    if out_path.exists() or out_path.is_symlink():
        return out_path
    if not _is_twitch_id(vod_id):
        raise FileNotFoundError(
            f"Local VOD not found: {out_path}  "
            f"(set up the symlink via the GUI's Change VOD dialog)"
        )
    url = f"https://www.twitch.tv/videos/{vod_id}"
    subprocess.run(
        ["yt-dlp", "-f", "best", "-o", str(out_path), url],
        check=True,
    )
    return out_path


def _is_twitch_id(vod_id: str) -> bool:
    return vod_id.strip().isdigit()


def _extract_twitch_numeric_id(vod_id: str) -> str | None:
    """Return the numeric Twitch VOD ID from a plain or dash-prefixed ID.

    Handles both plain numeric IDs ("2782428357") and the compound format
    produced by some download pipelines ("2782428357-524540406-<uuid>").
    Returns None if no numeric Twitch ID can be extracted.
    """
    part = vod_id.strip().split("-")[0]
    return part if part.isdigit() else None


def download_chat(vod_id: str, out_dir: Path) -> list[ChatMessage]:
    """Returns chat messages with offsets in seconds from VOD start."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{vod_id}_chat.json"

    if not json_path.exists():
        twitch_id = _extract_twitch_numeric_id(vod_id)
        if not twitch_id:
            return []   # local file — no chat to download
        subprocess.run(
            [
                "TwitchDownloaderCLI", "chatdownload",
                "--id", twitch_id,
                "-o", str(json_path),
            ],
            check=True,
        )

    if not json_path.exists():
        return []

    data = json.loads(json_path.read_text(encoding="utf-8"))
    msgs = []
    for c in data.get("comments", []):
        msgs.append(ChatMessage(
            offset_sec=float(c["content_offset_seconds"]),
            user=c["commenter"]["display_name"],
            text=c["message"]["body"],
        ))
    msgs.sort(key=lambda m: m.offset_sec)
    return msgs
