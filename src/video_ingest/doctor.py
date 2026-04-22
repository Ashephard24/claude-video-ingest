"""Doctor: diagnostic checks for the install."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import library_root
from .utils import check_command, get_version


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_checks() -> list[CheckResult]:
    """Run all diagnostic checks. Returns a list of results."""
    results: list[CheckResult] = []

    # Python version
    v = sys.version_info
    py_ok = (v.major, v.minor) >= (3, 10)
    results.append(
        CheckResult(
            name="Python version",
            ok=py_ok,
            detail=f"{v.major}.{v.minor}.{v.micro}" + ("" if py_ok else " (need 3.10+)"),
        )
    )

    # yt-dlp (as library)
    try:
        yt_dlp = importlib.import_module("yt_dlp")
        yt_version = getattr(yt_dlp, "version", None)
        version_str = getattr(yt_version, "__version__", None) or "installed"
        results.append(
            CheckResult(
                name="yt-dlp (Python package)",
                ok=True,
                detail=str(version_str),
            )
        )
    except ImportError:
        results.append(
            CheckResult(
                name="yt-dlp (Python package)",
                ok=False,
                detail="not installed — run: pip install yt-dlp",
            )
        )

    # ffmpeg
    ffmpeg_path = check_command("ffmpeg")
    ffmpeg_version = get_version("ffmpeg") if ffmpeg_path else None
    results.append(
        CheckResult(
            name="ffmpeg",
            ok=bool(ffmpeg_path),
            detail=(ffmpeg_version or "installed") if ffmpeg_path else (
                "not installed — macOS: brew install ffmpeg | "
                "Ubuntu: sudo apt install ffmpeg | Windows: winget install ffmpeg"
            ),
        )
    )

    # Library folder
    root = library_root()
    results.append(
        CheckResult(
            name="Library folder",
            ok=True,
            detail=f"will live at: {root}" + (" (exists)" if root.exists() else " (will be created on first use)"),
        )
    )

    # Rich (for pretty CLI output)
    try:
        importlib.import_module("rich")
        results.append(CheckResult(name="rich (CLI output)", ok=True, detail="installed"))
    except ImportError:
        results.append(
            CheckResult(
                name="rich (CLI output)",
                ok=False,
                detail="not installed — run: pip install rich",
            )
        )

    # Whisper (optional)
    try:
        importlib.import_module("faster_whisper")
        results.append(
            CheckResult(
                name="faster-whisper (optional fallback)",
                ok=True,
                detail="installed",
                required=False,
            )
        )
    except ImportError:
        results.append(
            CheckResult(
                name="faster-whisper (optional fallback)",
                ok=False,
                detail="not installed (only needed if a video has no captions) — pip install faster-whisper",
                required=False,
            )
        )

    return results


def all_required_passing(results: list[CheckResult]) -> bool:
    return all(r.ok for r in results if r.required)
