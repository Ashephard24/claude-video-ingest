# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Claude Video Ingest.

Builds a single binary per platform:
  Windows: dist/ClaudeVideoIngest.exe
  macOS:   dist/ClaudeVideoIngest.app (bundle) / ClaudeVideoIngest (binary)
  Linux:   dist/ClaudeVideoIngest (ELF binary)

Bundled:
  - Python interpreter
  - All project runtime deps: PySide6, faster-whisper, yt-dlp, rich,
    imageio-ffmpeg (with its static ffmpeg binary per-platform)
  - The video_ingest package source

NOT bundled:
  - Whisper models — faster-whisper downloads them on first use to
    ~/.cache/huggingface/. Keeps the binary smaller (~500MB vs ~700MB)
    and avoids shipping redundant model files. First transcription with
    a new model size triggers a one-time ~150MB download.

To build locally:
    pip install -e ".[build]"
    pyinstaller build.spec --clean --noconfirm

Output lands in ./dist/ClaudeVideoIngest{.exe,.app,}/

Invoked by GitHub Actions CI via the same command (see .github/workflows/build.yml).
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ---------------------------------------------------------------------------
# Entry point + search paths
# ---------------------------------------------------------------------------

# Since this spec lives at the repo root alongside pyproject.toml, src/ is
# relative to here.
ROOT = Path(SPECPATH)  # SPECPATH is set by PyInstaller at parse time
SRC = ROOT / "src"

# CLI + GUI share one binary. cli.py's main() branches to gui_main() when
# invoked with no args (see cli.py:_no_cli_args_provided).
entry_script = str(SRC / "video_ingest" / "cli.py")


# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------

# PyInstaller's static analysis misses these because they're imported
# lazily inside functions. Collecting them explicitly.
hidden_imports = [
    # faster-whisper pulls in CTranslate2 dynamically
    "faster_whisper",
    "ctranslate2",
    # yt-dlp has many extractor modules loaded by name
    *collect_submodules("yt_dlp.extractor"),
    # Our own GUI subpackage — CLI imports it conditionally, PyInstaller
    # doesn't follow the conditional path without this hint.
    "video_ingest.gui.app",
    "video_ingest.gui.settings",
    "video_ingest.gui.settings_dialog",
    "video_ingest.gui.error_dialog",
    "video_ingest.gui.update_checker",
]


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

# imageio-ffmpeg ships a per-platform static ffmpeg binary that needs to
# be copied into the bundle.
datas = []
datas += collect_data_files("imageio_ffmpeg")

# faster-whisper / ctranslate2 have no data files that need explicit
# collection — hiddenimports + the default binary-deps analysis is enough.


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    [entry_script],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Excludes — saves ~100MB by not bundling these even if they happen to
    # be importable in the build env. None of our runtime code uses them.
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "pytest",
        "sphinx",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# ---------------------------------------------------------------------------
# Build configuration (one-file mode)
# ---------------------------------------------------------------------------

# One-file: a single distributable exe. Larger launch time (unpacks on
# each run), smaller distribution surface (one file to upload, one file
# for the user to download). Right tradeoff for an end-user GUI app.

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ClaudeVideoIngest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression can trigger antivirus false positives on Windows
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed mode: don't open a console window on Windows double-click.
    # The dual-mode entry still shows a console when invoked from an
    # existing terminal, which is what power users want.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# ---------------------------------------------------------------------------
# macOS .app bundle (only applied on Darwin)
# ---------------------------------------------------------------------------

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="ClaudeVideoIngest.app",
        icon=None,  # Icon file path goes here later; see M12 docs.
        bundle_identifier="com.aidan-shephard.claude-video-ingest",
        info_plist={
            "NSHighResolutionCapable": "True",
            "CFBundleShortVersionString": "2.0.0",
            "CFBundleVersion": "2.0.0",
        },
    )
