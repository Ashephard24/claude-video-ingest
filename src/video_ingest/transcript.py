"""
Transcript handling: parse VTT (from yt-dlp), emit SRT + plain text.
Falls back to Whisper if no captions are available and the user opts in.

The Whisper fallback uses `faster-whisper` (CTranslate2 backend) rather
than OpenAI's reference `whisper` package. Same models, no PyTorch
dependency, notably faster on CPU, and small enough to bundle into a
PyInstaller-produced binary. The function signature of `run_whisper`
is preserved from v1.x so the rest of the codebase is unaffected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import TranscriptError
from .utils import check_command

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single timed chunk of transcript."""

    start_seconds: float
    end_seconds: float
    text: str


def parse_vtt(vtt_path: Path) -> list[TranscriptSegment]:
    """
    Parse a WebVTT file into segments.

    YouTube auto-caption VTT files have rolling captions: each cue shows
    2-3 lines where the top line repeats the previous cue's bottom line.
    Example:
        [0-2s]  "Hello there"
        [2-4s]  "Hello there\\nand welcome"
        [4-6s]  "and welcome\\nto the show"

    We deduplicate at the LINE level, not the cue level, and attach each
    unique line to the timestamp of the cue where it first appeared.
    """
    content = vtt_path.read_text(encoding="utf-8", errors="replace")

    timestamp_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
    )

    # Stage 1: parse all cues (timestamp + list of text lines, with inline tags stripped)
    cues: list[tuple[float, float, list[str]]] = []
    blocks = re.split(r"\n\s*\n", content)

    for block in blocks:
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines:
            continue

        ts_match = None
        ts_idx = -1
        for i, line in enumerate(lines):
            m = timestamp_re.search(line)
            if m:
                ts_match = m
                ts_idx = i
                break
        if not ts_match:
            continue

        start = (
            int(ts_match.group(1)) * 3600
            + int(ts_match.group(2)) * 60
            + int(ts_match.group(3))
            + int(ts_match.group(4)) / 1000
        )
        end = (
            int(ts_match.group(5)) * 3600
            + int(ts_match.group(6)) * 60
            + int(ts_match.group(7))
            + int(ts_match.group(8)) / 1000
        )

        # Strip inline tags from each text line
        text_lines = []
        for raw in lines[ts_idx + 1 :]:
            stripped = re.sub(r"<[^>]+>", "", raw)
            stripped = re.sub(r"\s+", " ", stripped).strip()
            if stripped:
                text_lines.append(stripped)

        if text_lines:
            cues.append((start, end, text_lines))

    # Stage 2: dedupe at the line level. For each cue, take only lines we
    # haven't emitted before. Attach them to this cue's start time.
    segments: list[TranscriptSegment] = []
    seen_lines: set[str] = set()

    for start, end, text_lines in cues:
        new_lines = [ln for ln in text_lines if ln not in seen_lines]
        for ln in new_lines:
            seen_lines.add(ln)
        if new_lines:
            segments.append(
                TranscriptSegment(start, end, " ".join(new_lines))
            )

    return segments


def segments_to_srt(segments: list[TranscriptSegment]) -> str:
    """Emit segments as an SRT file."""
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(seg.start_seconds)} --> {_format_srt_time(seg.end_seconds)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def segments_to_plain_text(segments: list[TranscriptSegment], include_timestamps: bool = False) -> str:
    """Emit segments as plain text, one segment per line."""
    if include_timestamps:
        return "\n".join(
            f"[{_format_timestamp_short(seg.start_seconds)}] {seg.text}" for seg in segments
        )
    return "\n".join(seg.text for seg in segments)


def segments_to_markdown(
    segments: list[TranscriptSegment],
    video_title: str,
    creator: str,
) -> str:
    """
    Emit segments as a markdown document with a clear heading and
    bolded timestamps. Easier for Claude to parse than flat text.
    """
    lines = [
        f"# Transcript: {video_title}",
        "",
        f"Creator: {creator}",
        "",
        "Each line below is a segment of the video's transcript, with the",
        "timestamp it begins at. Use these timestamps when referring to",
        "specific moments in the video.",
        "",
        "---",
        "",
    ]
    for seg in segments:
        ts = _format_timestamp_short(seg.start_seconds)
        lines.append(f"**[{ts}]** {seg.text}")
        lines.append("")
    return "\n".join(lines)


def _format_srt_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_timestamp_short(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def run_whisper(audio_or_video_path: Path, model: str = "base") -> list[TranscriptSegment]:
    """
    Fallback: transcribe audio using faster-whisper (CTranslate2 backend).

    Imports faster_whisper lazily because it's a large optional dependency.
    Function signature and return shape are preserved from the v1.x
    openai-whisper implementation — the rest of the codebase is unaffected
    by the backend swap.

    The model argument accepts the same names the CLI exposes (tiny, base,
    small, medium, large). "large" is mapped to "large-v3" because that's
    the actual latest-large model name in faster-whisper's catalog.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise TranscriptError(
            what="Whisper fallback requested but faster-whisper isn't installed.",
            fix=[
                "Install it with: pip install faster-whisper",
                "Or skip Whisper by finding a video with captions.",
            ],
        ) from e

    if not check_command("ffmpeg"):
        raise TranscriptError(
            what="Whisper requires ffmpeg, which is not installed.",
            fix="Install ffmpeg (see README for platform-specific instructions).",
        )

    # Map the CLI's "large" alias to faster-whisper's actual model name.
    # All other names pass through unchanged.
    model_name = "large-v3" if model == "large" else model

    try:
        # CPU + int8 is the right default for bundled end-user binaries:
        # no GPU assumption, smallest memory footprint, good enough accuracy
        # at the model sizes we ship. Power users can't change this without
        # a source edit, which is fine — it's an implementation detail.
        model_obj = WhisperModel(model_name, device="cpu", compute_type="int8")
        # faster-whisper returns (segments_iterator, info). The iterator is
        # lazy — actual transcription happens as we consume it.
        segments_iter, _info = model_obj.transcribe(str(audio_or_video_path))
    except Exception as e:  # noqa: BLE001
        raise TranscriptError(
            what=f"Whisper failed to transcribe: {e}",
            fix="Try re-running. If it keeps failing, the audio track may be corrupt or missing.",
        ) from e

    segments: list[TranscriptSegment] = []
    try:
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    start_seconds=float(seg.start),
                    end_seconds=float(seg.end),
                    text=text,
                )
            )
    except Exception as e:  # noqa: BLE001
        # Iteration-time errors (e.g. decode failures partway through) land here.
        raise TranscriptError(
            what=f"Whisper failed while transcribing: {e}",
            fix="Try re-running. If it keeps failing, the audio track may be corrupt or missing.",
        ) from e

    if not segments:
        raise TranscriptError(
            what="Whisper produced an empty transcript.",
            fix="The audio may be silent or unintelligible. Try a different video.",
        )
    return segments
