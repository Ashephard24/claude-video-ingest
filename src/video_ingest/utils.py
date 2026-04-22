"""Small utilities: URL parsing, slug creation, dependency checking."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys as _sys
from urllib.parse import parse_qs, urlparse

from .errors import DependencyMissingError, InvalidURLError

# Windows-only: suppress console windows from subprocess calls made by
# the packaged binary. No effect on macOS/Linux.
if _sys.platform == "win32":
    _SUBPROCESS_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _SUBPROCESS_NO_WINDOW_FLAGS = 0

# Matches youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID, youtube.com/embed/ID
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_url(url: str) -> str:
    """
    Extract the 11-character video ID from a YouTube URL.

    Raises InvalidURLError if the URL isn't recognizable.
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError(
            what="No URL provided.",
            fix="Pass a YouTube URL as the first argument: video-ingest <url>",
        )

    url = url.strip()

    # Bare video ID passed in?
    if _VIDEO_ID_RE.match(url):
        return url

    try:
        parsed = urlparse(url)
    except ValueError:
        raise InvalidURLError(
            what=f"Could not parse URL: {url}",
            fix="Make sure it's a valid YouTube URL like https://youtube.com/watch?v=...",
        )

    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise InvalidURLError(
            what=f"Not a YouTube URL: {url}",
            fix=[
                "This tool only works with YouTube URLs.",
                "Supported formats: youtube.com/watch?v=..., youtu.be/..., youtube.com/shorts/...",
            ],
        )

    # youtu.be/VIDEO_ID
    if host in {"youtu.be", "www.youtu.be"}:
        vid = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(vid):
            return vid

    # youtube.com/watch?v=VIDEO_ID
    if parsed.path == "/watch":
        query = parse_qs(parsed.query)
        vids = query.get("v")
        if vids and _VIDEO_ID_RE.match(vids[0]):
            return vids[0]

    # youtube.com/shorts/VIDEO_ID or /embed/VIDEO_ID or /live/VIDEO_ID
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live", "v"}:
        if _VIDEO_ID_RE.match(path_parts[1]):
            return path_parts[1]

    raise InvalidURLError(
        what=f"Couldn't find a video ID in URL: {url}",
        fix="Make sure the URL points to a specific video, not a channel or playlist.",
    )


def slugify(text: str, max_length: int = 60) -> str:
    """
    Turn an arbitrary string into a filesystem-safe slug.

    Handles unicode by transliterating to ASCII-ish, lowercases,
    replaces non-alphanumerics with hyphens, collapses runs of hyphens.
    """
    if not text:
        return "untitled"

    # Normalize: replace common punctuation with spaces, then collapse
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")

    if not text:
        return "untitled"

    if len(text) > max_length:
        # Truncate at a word boundary if possible
        text = text[:max_length].rsplit("-", 1)[0] or text[:max_length]

    return text


def format_duration(seconds: float | int | None) -> str:
    """Format seconds as HH:MM:SS or MM:SS."""
    if seconds is None:
        return "unknown"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def seconds_to_timestamp(seconds: float) -> str:
    """Format seconds as HH-MM-SS for use in filenames."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}-{m:02d}-{s:02d}"


def _imageio_ffmpeg_path() -> str | None:
    """
    Return the path to ffmpeg bundled inside the imageio-ffmpeg package,
    or None if imageio-ffmpeg isn't installed.

    This is the fallback used by the packaged binary — PyInstaller bundles
    imageio-ffmpeg which ships a static ffmpeg binary per-platform. In
    development environments the system ffmpeg takes precedence.
    """
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        return None
    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None
    return path or None


def check_command(name: str) -> str | None:
    """
    Return the path to a binary, or None if not available.

    For 'ffmpeg', falls back to imageio-ffmpeg's bundled binary when the
    system PATH doesn't have it. This lets the packaged binary work
    without requiring users to install ffmpeg themselves.
    """
    path = shutil.which(name)
    if path:
        return path
    if name == "ffmpeg":
        return _imageio_ffmpeg_path()
    return None


def require_command(name: str, install_hints: dict[str, str] | None = None) -> str:
    """
    Verify a binary is on PATH, or raise DependencyMissingError with
    platform-specific install instructions.
    """
    path = check_command(name)
    if path:
        return path

    hints = install_hints or {}
    fix_lines = [f"Install {name} and make sure it's on your PATH."]
    if hints:
        fix_lines.append("")
        fix_lines.append("Install commands:")
        for platform, cmd in hints.items():
            fix_lines.append(f"  {platform}: {cmd}")

    raise DependencyMissingError(
        what=f"Required tool not found: {name}",
        fix=fix_lines,
    )


def get_version(name: str) -> str | None:
    """Get the version of a binary by running `<name> --version`."""
    path = check_command(name)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_SUBPROCESS_NO_WINDOW_FLAGS,
        )
        output = (result.stdout or result.stderr).strip().split("\n")[0]
        return output if output else None
    except (subprocess.SubprocessError, OSError):
        return None
