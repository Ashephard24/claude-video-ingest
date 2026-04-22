"""
GUI settings persistence.

Reads/writes a small JSON file at the platform config location:
  Windows: %APPDATA%\\Claude Video Ingest\\settings.json
  macOS:   ~/Library/Application Support/Claude Video Ingest/settings.json
  Linux:   ~/.config/Claude Video Ingest/settings.json

Defaults mirror the v1.2.2 CLI defaults exactly, so a brand-new install
produces byte-identical output to the CLI with no settings passed.

The settings surface is deliberately small — only the things end users
are likely to want to change. Power users who need finer control can
use the CLI (dual-mode binary).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from PySide6.QtCore import QStandardPaths

logger = logging.getLogger(__name__)


# Whisper model choices exposed in the Settings dropdown.
# Order matters — the UI renders them in this order, biggest→smallest
# intentionally so "accuracy-first" is the top choice but "base" is
# pre-selected as the default (matching CLI behavior).
WHISPER_MODEL_CHOICES: list[tuple[str, str]] = [
    ("tiny", "Tiny (~75 MB) — fastest, least accurate"),
    ("base", "Base (~140 MB) — default, balanced"),
    ("small", "Small (~460 MB) — more accurate, slower"),
    ("medium", "Medium (~1.5 GB) — high accuracy, much slower"),
    ("large", "Large v3 (~3 GB) — best accuracy, very slow"),
]


@dataclass
class GuiSettings:
    """
    User-editable settings. Exactly the fields surfaced in the Settings
    dialog — not the full pipeline configuration.
    """
    max_frames: int = 60
    whisper_model: str = "base"
    use_whisper_fallback: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "GuiSettings":
        """Parse JSON into a GuiSettings, ignoring unknown keys and
        falling back to defaults for missing ones. Robust to older
        or newer settings files."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("settings.json unreadable; using defaults")
            return cls()
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**filtered)
        except TypeError as e:
            logger.warning("settings.json type mismatch (%s); using defaults", e)
            return cls()


def settings_path() -> Path:
    """
    Return the platform-appropriate path to settings.json, creating the
    containing directory if needed.
    """
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    # Qt may return an empty string on some exotic platforms; fall back to
    # a sensible default.
    if not base:
        base = str(Path.home() / ".config" / "Claude Video Ingest")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path / "settings.json"


def load_settings() -> GuiSettings:
    """Load settings from disk. Returns defaults if the file is missing."""
    path = settings_path()
    if not path.exists():
        return GuiSettings()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read settings.json (%s); using defaults", e)
        return GuiSettings()
    return GuiSettings.from_json(text)


def save_settings(settings: GuiSettings) -> None:
    """Persist settings atomically."""
    path = settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(settings.to_json(), encoding="utf-8")
    tmp.replace(path)
