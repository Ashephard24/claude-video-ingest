"""Paths: single source of truth for where files live on disk."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def library_root() -> Path:
    """
    The root of the Claude video library.

    Resolution priority (highest first):
      1. VIDEO_INGEST_LIBRARY environment variable
      2. ``library_location`` in the GUI settings.json
      3. Default: ~/Documents/claude-video-library/

    NOT CACHED — resolved fresh on every call so that a Settings-dialog
    change takes effect immediately for subsequent library operations.
    """
    override = os.environ.get("VIDEO_INGEST_LIBRARY")
    if override:
        return Path(override).expanduser().resolve()
    from_settings = _settings_library_location()
    if from_settings is not None:
        return from_settings.expanduser().resolve()
    return Path.home() / "Documents" / "claude-video-library"


def _settings_config_dir() -> Path:
    """
    Return the platform-appropriate config directory for the GUI's
    settings.json, matching QStandardPaths.AppConfigLocation. Uses
    stdlib only so paths.py stays importable in CLI-only installs
    (no PySide6 dependency).

    Resolved layout (no organization name set, app name = "Claude Video Ingest"):
      Windows: %LOCALAPPDATA%\\Claude Video Ingest
      macOS:   ~/Library/Preferences/Claude Video Ingest
      Linux:   $XDG_CONFIG_HOME/Claude Video Ingest  (or ~/.config/... if unset)
    """
    app_name = "Claude Video Ingest"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / app_name
        return Path.home() / "AppData" / "Local" / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / app_name
    # Linux / other POSIX: respect XDG_CONFIG_HOME, else ~/.config
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / app_name
    return Path.home() / ".config" / app_name


def _settings_library_location() -> Path | None:
    """
    Read ``library_location`` from settings.json, if present and set.
    Returns None on any failure (file missing, unreadable, malformed,
    or the field empty). Silent — the caller falls through to default.

    paths.py is used by the CLI too; this function must not import from
    the gui/ subpackage.
    """
    path = _settings_config_dir() / "settings.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("library_location")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


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
