"""
Downloader: wraps yt-dlp.

Uses yt-dlp as a Python library (not subprocess) for cleaner error handling.
Falls back to subprocess only if needed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import NetworkError, TranscriptError, VideoUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    """What we care about from a video's metadata."""

    video_id: str
    title: str
    creator: str
    duration_seconds: int
    upload_date: str | None  # YYYYMMDD from yt-dlp
    description: str
    url: str
    thumbnail_url: str | None
    has_captions: bool
    caption_languages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "creator": self.creator,
            "duration_seconds": self.duration_seconds,
            "upload_date": self.upload_date,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url,
            "has_captions": self.has_captions,
            "caption_languages": self.caption_languages,
            "description": self.description,
        }


def _classify_ytdlp_error(error: Exception) -> Exception:
    """Turn a yt-dlp error into one of our user-facing exceptions."""
    msg = str(error).lower()

    if "private video" in msg or "this video is private" in msg:
        return VideoUnavailableError(
            what="This video is private.",
            fix="Only the owner can access private videos. Nothing to do here.",
        )
    if "video unavailable" in msg or "removed" in msg or "terminated" in msg:
        return VideoUnavailableError(
            what="Video is unavailable (removed, terminated, or region-locked).",
            fix="Check that the URL opens in a browser. If it does, the issue may be region-locking.",
        )
    if "members-only" in msg or "sign in to confirm" in msg or "age" in msg:
        return VideoUnavailableError(
            what="Video requires authentication (age-restricted or members-only).",
            fix="This tool doesn't support authenticated downloads. Use a publicly accessible video.",
        )
    if "urlopen error" in msg or "network" in msg or "connection" in msg or "timed out" in msg:
        return NetworkError(
            what="Could not reach YouTube.",
            fix=[
                "Check your internet connection.",
                "Run the command again (often transient).",
                "If it keeps failing, run: video-ingest --doctor",
            ],
        )
    # Fallback: surface the raw message but mark it as a video-unavailable case
    return VideoUnavailableError(
        what=f"yt-dlp reported: {error}",
        fix="Check that the URL opens in a browser. If it does, try running the command again.",
    )


def fetch_metadata(url: str) -> VideoMetadata:
    """Fetch video metadata without downloading the video."""
    try:
        import yt_dlp
    except ImportError as e:
        raise NetworkError(
            what="yt-dlp Python package not installed.",
            fix="Install it with: pip install yt-dlp",
        ) from e

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise _classify_ytdlp_error(e) from e
    except Exception as e:  # noqa: BLE001
        raise _classify_ytdlp_error(e) from e

    if not info:
        raise VideoUnavailableError(
            what="yt-dlp returned no information for this video.",
            fix="Check that the URL is correct and the video is public.",
        )

    # Caption languages: check both manual subtitles and auto-captions
    manual_subs = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}
    caption_langs = sorted(set(list(manual_subs.keys()) + list(auto_subs.keys())))

    return VideoMetadata(
        video_id=info.get("id", ""),
        title=info.get("title", "Untitled"),
        creator=info.get("uploader") or info.get("channel") or "Unknown",
        duration_seconds=int(info.get("duration") or 0),
        upload_date=info.get("upload_date"),
        description=(info.get("description") or "").strip(),
        url=info.get("webpage_url") or url,
        thumbnail_url=info.get("thumbnail"),
        has_captions=bool(manual_subs or auto_subs),
        caption_languages=caption_langs,
    )


def download_video_and_subs(
    url: str,
    output_dir: Path,
    prefer_language: str = "en",
) -> tuple[Path, Path | None, bool]:
    """
    Download the video (360p max) and subtitles to output_dir.

    Returns (video_path, subtitle_path_or_none, subtitle_is_auto_generated).

    The subtitle path will be None if no captions are available —
    the caller should fall back to Whisper if desired.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise NetworkError(
            what="yt-dlp Python package not installed.",
            fix="Install it with: pip install yt-dlp",
        ) from e

    output_dir.mkdir(parents=True, exist_ok=True)

    # 360p or lower, prefer mp4. Falls back gracefully if 360p unavailable.
    ydl_opts = {
        "format": "best[height<=360][ext=mp4]/best[height<=360]/worst",
        "outtmpl": str(output_dir / "video.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [prefer_language, "en"],
        "subtitlesformat": "vtt",
        "postprocessors": [],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise _classify_ytdlp_error(e) from e
    except Exception as e:  # noqa: BLE001
        raise _classify_ytdlp_error(e) from e

    # Find the downloaded video file
    video_files = list(output_dir.glob("video.*"))
    video_files = [f for f in video_files if f.suffix.lower() in {".mp4", ".webm", ".mkv", ".m4v"}]
    if not video_files:
        raise VideoUnavailableError(
            what="Video downloaded but file not found on disk.",
            fix="Re-run the command. If it fails again, run: video-ingest --doctor",
        )
    video_path = video_files[0]

    # Find subtitle file: prefer manual over auto, prefer requested language
    subtitle_path = None
    is_auto = False
    for lang in [prefer_language, "en"]:
        # Manual subs first
        manual = list(output_dir.glob(f"video.{lang}.vtt"))
        if manual:
            subtitle_path = manual[0]
            is_auto = False
            break
        # Then auto-captions (yt-dlp names these without a special marker;
        # if manual subs don't exist but automatic do, they'll be at the same path)
    # If no language-specific file, grab any vtt
    if subtitle_path is None:
        any_vtt = list(output_dir.glob("video.*.vtt"))
        if any_vtt:
            subtitle_path = any_vtt[0]
            is_auto = True  # assume auto if we didn't find it by language

    return video_path, subtitle_path, is_auto
