"""
Frame extraction using ffmpeg.

Two-pass approach:
  1. Use ffmpeg's scene filter to detect visual changes above a threshold
  2. Cap the total number of frames (default 60) by keeping the most
     distinct ones if we exceed the cap
  3. Enforce a floor: no gap between frames longer than `min_interval`
     (default 30s) — if scene detection didn't produce enough, we
     backfill with evenly-spaced frames

This produces a rich but bounded set of frames for any video length.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import FrameExtractionError
from .utils import check_command, require_command, seconds_to_timestamp

logger = logging.getLogger(__name__)


@dataclass
class ExtractedFrame:
    """A single frame on disk with its timestamp."""

    timestamp_seconds: float
    path: Path


def _run_ffmpeg(args: list[str], description: str) -> subprocess.CompletedProcess:
    """Run an ffmpeg command with sensible error handling."""
    ffmpeg = require_command(
        "ffmpeg",
        install_hints={
            "macOS": "brew install ffmpeg",
            "Ubuntu/Debian": "sudo apt install ffmpeg",
            "Windows": "winget install ffmpeg  (or download from ffmpeg.org)",
        },
    )
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", *args],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise FrameExtractionError(
            what=f"ffmpeg timed out during: {description}",
            fix="Try a shorter video, or re-run (may be transient).",
        ) from e

    if result.returncode != 0:
        raise FrameExtractionError(
            what=f"ffmpeg failed during: {description}",
            fix=[
                f"ffmpeg error: {result.stderr.strip()[:500]}",
                "Try re-running. If it keeps failing, the video may be corrupt.",
            ],
        )
    return result


def _detect_scene_timestamps(
    video_path: Path,
    threshold: float = 0.35,
) -> list[float]:
    """
    Run ffmpeg with the scene filter, parse timestamps of detected scene changes.

    Threshold 0.0–1.0; higher = fewer scenes. 0.35 is a reasonable default
    for tutorial content that balances signal vs. noise.
    """
    ffmpeg = require_command("ffmpeg")
    # Use showinfo + scene select; parse pts_time from stderr
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(video_path),
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    # showinfo writes lines like: [Parsed_showinfo_1 @ ...] n:  0 pts: 123 pts_time:4.120 ...
    timestamps: list[float] = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            try:
                after = line.split("pts_time:", 1)[1].strip()
                ts_str = after.split()[0].rstrip(",")
                timestamps.append(float(ts_str))
            except (IndexError, ValueError):
                continue
    return sorted(set(timestamps))


def _select_frame_timestamps(
    scene_timestamps: list[float],
    video_duration: float,
    max_frames: int,
    min_interval: float,
) -> list[float]:
    """
    Choose which timestamps to actually extract frames at.

    Combines scene detection with a time-interval floor so we never
    go too long without a frame, then caps at max_frames.
    """
    # Always include a frame near the start (1 second in, to skip intros/black frames)
    candidates: list[float] = [1.0]
    candidates.extend(scene_timestamps)

    # Enforce min_interval floor: walk through and add interpolated timestamps
    # wherever the gap is too large.
    candidates = sorted(set(candidates))
    filled: list[float] = []
    prev = 0.0
    for ts in candidates:
        while ts - prev > min_interval:
            prev += min_interval
            filled.append(prev)
        filled.append(ts)
        prev = ts
    # Fill the tail up to duration
    while video_duration - prev > min_interval:
        prev += min_interval
        filled.append(prev)

    # Deduplicate and clamp
    filled = sorted({round(ts, 2) for ts in filled if 0 < ts < video_duration})

    # Cap: if we have too many, keep an evenly-distributed subset that
    # preserves the first and last.
    if len(filled) > max_frames:
        step = len(filled) / max_frames
        chosen: list[float] = []
        for i in range(max_frames):
            idx = min(int(i * step), len(filled) - 1)
            chosen.append(filled[idx])
        filled = sorted(set(chosen))

    return filled


def _extract_single_frame(video_path: Path, timestamp: float, output_path: Path) -> None:
    """Extract a single frame at the given timestamp to output_path."""
    _run_ffmpeg(
        [
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",  # JPEG quality: 1=best, 31=worst. 3 = very good, small files.
            "-y",
            str(output_path),
        ],
        description=f"extracting frame at {timestamp:.2f}s",
    )


def extract_frames(
    video_path: Path,
    output_dir: Path,
    video_duration: float,
    max_frames: int = 60,
    min_interval: float = 30.0,
    scene_threshold: float = 0.35,
    progress_callback=None,
) -> list[ExtractedFrame]:
    """
    Extract frames from a video using scene detection + interval floor.

    progress_callback(current, total) is called after each frame, if provided.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pass 1: scene detection
    scene_timestamps = _detect_scene_timestamps(video_path, scene_threshold)
    logger.info("Scene detection found %d candidates", len(scene_timestamps))

    # Pass 2: pick the actual frames
    chosen = _select_frame_timestamps(
        scene_timestamps,
        video_duration=video_duration,
        max_frames=max_frames,
        min_interval=min_interval,
    )

    if not chosen:
        raise FrameExtractionError(
            what="No frames could be selected for extraction.",
            fix="The video may be too short or have no detectable content. Try a different video.",
        )

    # Pass 3: extract
    frames: list[ExtractedFrame] = []
    total = len(chosen)
    for i, ts in enumerate(chosen, start=1):
        filename = f"{seconds_to_timestamp(ts)}.jpg"
        path = output_dir / filename
        _extract_single_frame(video_path, ts, path)
        frames.append(ExtractedFrame(timestamp_seconds=ts, path=path))
        if progress_callback:
            progress_callback(i, total)

    return frames
