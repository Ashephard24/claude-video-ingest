"""Paths: single source of truth for where files live on disk."""

from __future__ import annotations

import os
from pathlib import Path


def library_root() -> Path:
    """
    The root of the Claude video library.

    Defaults to ~/Documents/claude-video-library/ but can be overridden with
    the VIDEO_INGEST_LIBRARY environment variable (useful for testing).
    """
    override = os.environ.get("VIDEO_INGEST_LIBRARY")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Documents" / "claude-video-library"


def library_index_path() -> Path:
    """Path to the master library index markdown file (human-facing)."""
    return library_root() / "library.md"


def library_json_path() -> Path:
    """
    Path to the machine-readable mirror of the library index.

    Used by the GUI's Library view. The markdown file at library_index_path()
    remains the human-facing document; this JSON is a sidecar kept in sync
    by update_library_index() and reconcile_library_index().
    """
    return library_root() / "library.json"


def error_log_path() -> Path:
    """Where we dump full tracebacks for unexpected failures."""
    return library_root() / ".last-error.log"


def ensure_library_root() -> Path:
    """Create the library root if it doesn't exist, and return it."""
    root = library_root()
    root.mkdir(parents=True, exist_ok=True)
    return root
