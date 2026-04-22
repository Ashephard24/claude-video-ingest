"""
Custom exceptions. Each one carries a user-facing message that explains
what went wrong AND what to do about it. No cryptic stack traces shown
to the user — those go to the error log.
"""

from __future__ import annotations


class VideoIngestError(Exception):
    """
    Base exception. All user-facing errors inherit from this.

    Subclasses should pass:
      - what: a short description of what went wrong
      - fix: one or more concrete steps the user can take

    The CLI catches this class specifically and renders a nice message.
    Anything that's NOT a VideoIngestError is treated as an unexpected
    crash and gets dumped to the error log.
    """

    def __init__(self, what: str, fix: str | list[str] | None = None):
        self.what = what
        self.fix = fix if isinstance(fix, list) else ([fix] if fix else [])
        super().__init__(what)


class DependencyMissingError(VideoIngestError):
    """A required system tool (yt-dlp, ffmpeg) isn't installed."""


class VideoUnavailableError(VideoIngestError):
    """Video is private, deleted, region-locked, or otherwise inaccessible."""


class NetworkError(VideoIngestError):
    """Couldn't reach YouTube. Probably transient."""


class TranscriptError(VideoIngestError):
    """Couldn't get a transcript and Whisper fallback wasn't available/enabled."""


class FrameExtractionError(VideoIngestError):
    """ffmpeg failed during frame extraction."""


class InvalidURLError(VideoIngestError):
    """The provided URL doesn't look like a YouTube URL."""


class IngestCancelled(Exception):
    """
    Raised when the pipeline detects a cancellation request at a step
    boundary. Deliberately NOT a subclass of VideoIngestError — this
    is not a user-facing failure, it's an expected control flow event
    triggered by the user clicking Cancel or Stop in the GUI.
    Callers should catch it separately and treat the ingest as cancelled
    rather than failed.
    """
