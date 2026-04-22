"""
Local build helper for Claude Video Ingest.

Run this to produce a binary on your current platform:
    python build.py

What it does:
    1. Verifies you're in a venv (prevents polluting system Python)
    2. Installs build deps if missing
    3. Cleans previous build artifacts
    4. Runs pyinstaller against build.spec
    5. Reports where the binary landed

For CI builds, GitHub Actions runs the same pyinstaller command directly
from .github/workflows/build.yml — this script exists only for local
pre-CI testing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent.resolve()
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def main() -> int:
    if not _in_virtualenv():
        print(
            "✗ Not in a virtual environment.\n"
            "  Create one first:\n"
            "      python -m venv .venv\n"
            "      .venv\\Scripts\\activate    (Windows)\n"
            "      source .venv/bin/activate   (macOS/Linux)"
        )
        return 1

    print("Installing build dependencies...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".[build]", "--quiet"],
        cwd=ROOT,
    )
    if r.returncode != 0:
        print("✗ pip install failed.")
        return r.returncode

    print("\nCleaning previous build artifacts...")
    for target in (DIST, BUILD):
        if target.exists():
            shutil.rmtree(target)
            print(f"  removed {target.name}/")

    print("\nRunning PyInstaller...")
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "build.spec",
            "--clean",
            "--noconfirm",
        ],
        cwd=ROOT,
    )
    if r.returncode != 0:
        print(f"\n✗ PyInstaller failed with exit code {r.returncode}.")
        return r.returncode

    # Locate the produced binary
    if sys.platform == "win32":
        binary = DIST / "ClaudeVideoIngest.exe"
    elif sys.platform == "darwin":
        binary = DIST / "ClaudeVideoIngest.app"
    else:
        binary = DIST / "ClaudeVideoIngest"

    if not binary.exists():
        print(f"\n✗ Expected binary at {binary} but it wasn't produced.")
        return 1

    size_mb = _size_mb(binary)
    print(f"\n✓ Build succeeded.")
    print(f"  Binary: {binary}")
    print(f"  Size:   {size_mb:.1f} MB")
    print(f"\nTo test: double-click {binary.name}, or run from a terminal with --doctor.")
    return 0


def _in_virtualenv() -> bool:
    """Detect venv or virtualenv."""
    return (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / (1024 * 1024)
    # macOS .app is a directory — sum recursively
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total / (1024 * 1024)


if __name__ == "__main__":
    raise SystemExit(main())
