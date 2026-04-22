"""
Pipeline: orchestrates the full ingestion flow.

Kept separate from the CLI so it can be imported and used
programmatically (e.g. from an MCP server later).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .downloader import VideoMetadata, download_video_and_subs, fetch_metadata
from .errors import IngestCancelled, TranscriptError
from .frames import ExtractedFrame, extract_frames
from .library import (
    LibraryEntry,
    compute_folder_name,
    reconcile_library_index,
    update_library_index,
    write_library_readme,
    write_video_folder,
)
from .paths import ensure_library_root
from .transcript import (
    TranscriptSegment,
    parse_vtt,
    run_whisper,
)
from .utils import format_duration

logger = logging.getLogger(__name__)


class CancelToken:
    """
    Cooperative cancellation signal. The GUI sets `cancelled = True`;
    the pipeline checks `is_cancelled()` at step boundaries and raises
    IngestCancelled if set.

    Not thread-safe in the strict sense — but a single-writer, single-reader
    boolean flag is safe in practice on CPython (GIL-protected attribute
    writes). Good enough for the 5-step-boundary use case.

    The CLI never creates one of these; cancellation is a GUI-only concept.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


class Progress:
    """
    Progress reporter. The CLI passes a Rich-backed implementation;
    library users can pass their own.

    Methods:
      step(current, total, label)  — report the current pipeline step
      substep(label)               — report a sub-action within the current step
      ok(message)                  — report step success
      warn(message)                — non-fatal issue (e.g., no captions → fallback)
      frame_progress(n, total)     — progress within frame extraction
    """

    def step(self, current: int, total: int, label: str) -> None: ...
    def substep(self, label: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def frame_progress(self, n: int, total: int) -> None: ...


class SilentProgress(Progress):
    """No-op progress reporter."""


def ingest(
    url: str,
    use_whisper_fallback: bool = True,
    whisper_model: str = "base",
    max_frames: int = 60,
    min_frame_interval: float = 30.0,
    scene_threshold: float = 0.35,
    batch_size: int = 18,
    progress: Progress | None = None,
    cancel_token: CancelToken | None = None,
) -> Path:
    """
    Ingest a single YouTube video. Returns the path to the created folder.

    Raises VideoIngestError (or a subclass) on any failure we've classified.
    Raises IngestCancelled if cancel_token.cancel() was called at a step
    boundary. Anything else bubbles up as an unexpected error for the CLI
    to catch and write to the error log.

    cancel_token is optional — CLI invocations pass None and skip the checks.
    """
    progress = progress or SilentProgress()

    def _check_cancel() -> None:
        if cancel_token is not None and cancel_token.is_cancelled():
            raise IngestCancelled()

    ensure_library_root()
    write_library_readme()

    # ---------------- STEP 1/5: metadata ----------------
    _check_cancel()
    progress.step(1, 5, "Fetching video metadata")
    metadata = fetch_metadata(url)
    progress.ok(
        f'"{metadata.title}" by {metadata.creator} — {format_duration(metadata.duration_seconds)}'
    )

    # Compute final folder now so we can detect existing ingests early
    folder_name = compute_folder_name(metadata)
    final_folder = ensure_library_root() / folder_name
    if final_folder.exists():
        progress.warn(f"Folder already exists — will overwrite: {folder_name}")
        shutil.rmtree(final_folder)

    # ---------------- STEP 2/5: download video + subs ----------------
    # Use a temp working directory; we only move final artifacts to the library
    with tempfile.TemporaryDirectory(prefix="video-ingest-") as tmpdir:
        tmp_path = Path(tmpdir)

        _check_cancel()
        progress.step(2, 5, "Downloading video (360p)")
        video_path, subtitle_path, sub_is_auto = download_video_and_subs(
            url, tmp_path, prefer_language="en"
        )
        progress.ok(f"Downloaded: {video_path.name}")

        # ---------------- STEP 3/5: transcript ----------------
        _check_cancel()
        progress.step(3, 5, "Extracting transcript")
        segments: list[TranscriptSegment]
        transcript_source: str

        if subtitle_path and subtitle_path.exists():
            try:
                segments = parse_vtt(subtitle_path)
                if not segments:
                    raise TranscriptError(
                        what="Caption file was empty after parsing.",
                        fix="Will try Whisper fallback if enabled.",
                    )
                transcript_source = (
                    "YouTube auto-captions" if sub_is_auto else "YouTube manual captions"
                )
                progress.ok(f"Got captions ({len(segments)} segments, {transcript_source})")
            except TranscriptError:
                segments = []
                transcript_source = ""
        else:
            progress.warn("No captions available for this video.")
            segments = []
            transcript_source = ""

        # Whisper fallback
        if not segments:
            if use_whisper_fallback:
                progress.substep(
                    f"Transcribing audio with Whisper ({whisper_model} model) — this may take a minute"
                )
                segments = run_whisper(video_path, model=whisper_model)
                transcript_source = f"Whisper ({whisper_model})"
                progress.ok(f"Whisper produced {len(segments)} segments")
            else:
                raise TranscriptError(
                    what="No captions available and Whisper fallback is disabled.",
                    fix=[
                        "Re-run with Whisper enabled (it's on by default):",
                        f"  video-ingest {url}",
                        "Or find a video with captions available.",
                    ],
                )

        # ---------------- STEP 4/5: frames ----------------
        _check_cancel()
        progress.step(4, 5, "Extracting frames (scene detection)")
        frames_output = tmp_path / "frames"
        frames: list[ExtractedFrame] = extract_frames(
            video_path,
            frames_output,
            video_duration=float(metadata.duration_seconds),
            max_frames=max_frames,
            min_interval=min_frame_interval,
            scene_threshold=scene_threshold,
            progress_callback=progress.frame_progress,
        )
        progress.ok(f"{len(frames)} frames extracted")

        # ---------------- STEP 5/5: write library ----------------
        _check_cancel()
        progress.step(5, 5, "Writing library files")
        final_folder.mkdir(parents=True, exist_ok=True)
        final_frames_dir = final_folder / "frames"
        final_frames_dir.mkdir(parents=True, exist_ok=True)

        # Copy frames to final location and update ExtractedFrame paths
        final_frames: list[ExtractedFrame] = []
        for frame in frames:
            dest = final_frames_dir / frame.path.name
            shutil.copy2(frame.path, dest)
            final_frames.append(ExtractedFrame(frame.timestamp_seconds, dest))

        write_video_folder(
            final_folder,
            metadata,
            segments,
            final_frames,
            transcript_source=transcript_source,
            batch_size=batch_size,
        )

        # Reconcile first to prune any rows for folders deleted since last ingest
        reconcile_library_index()

        # Update master index
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        update_library_index(
            LibraryEntry(
                ingest_date=today,
                title=metadata.title,
                creator=metadata.creator,
                duration=format_duration(metadata.duration_seconds),
                folder_name=folder_name,
                video_url=metadata.url,
            )
        )
        progress.ok(f"Folder: {final_folder}")

        # Report batching outcome so user knows what to expect
        from .library import plan_batches
        plan = plan_batches(final_frames, batch_size=batch_size)
        n = len(plan.frame_batches)
        if plan.batched:
            progress.ok(
                f"Batched upload: {n} batches. Drag START-HERE-for-Claude.md "
                f"into Claude first, then batch-1/, batch-2/, ... in order."
            )
        else:
            progress.ok(
                "Single-batch upload. Drag START-HERE-for-Claude.md into "
                "Claude first, then batch-1/ contents."
            )
        progress.ok("Library index updated")

    return final_folder
