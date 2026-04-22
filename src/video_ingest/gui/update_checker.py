"""
Update checker.

On GUI launch, asynchronously queries the GitHub Releases API for the
latest release of the repo and compares against __version__. If a newer
version exists, emits `update_available(latest_tag, release_url)`.

Non-blocking. Silent on failure. Caches the last-check timestamp so
we don't hammer the API — checks at most once per 24 hours.

Design notes:
  - Uses QNetworkAccessManager so the request integrates with Qt's event
    loop — no separate thread needed.
  - Comparison is semver-aware but tolerant: v2.0.0 > 2.0.0, v2.0.0-beta
    is treated as pre-release and skipped.
  - If the embedded version contains anything non-semver, we skip the
    check entirely. Safer than false positives.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QStandardPaths, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger(__name__)


# Hardcoded for now — change this line when you settle on a repo URL.
GITHUB_OWNER = "Ashephard24"
GITHUB_REPO = "claude-video-ingest"

# How long to wait between checks (24 hours in seconds).
CHECK_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass
class UpdateInfo:
    """Result of a successful check when an update is available."""
    latest_tag: str
    current_version: str
    release_url: str
    release_notes: str = ""


class UpdateChecker(QObject):
    """
    Async update check. Fire-and-forget — construct, call check(), and
    connect to update_available() if you care about the result.
    """

    update_available = Signal(object)  # UpdateInfo

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_reply_finished)

    def check(self, current_version: str, force: bool = False) -> None:
        """
        Start an update check. If `force` is False, skips the check when
        the last check was less than CHECK_INTERVAL_SECONDS ago.
        """
        if not _is_plain_semver(current_version):
            logger.debug("Skipping update check: version %r is non-semver", current_version)
            return

        if not force:
            last = _read_last_check_time()
            if last is not None and (time.time() - last) < CHECK_INTERVAL_SECONDS:
                logger.debug("Skipping update check: last check was %.0fs ago", time.time() - last)
                return

        url = QUrl(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        )
        request = QNetworkRequest(url)
        request.setRawHeader(QByteArray(b"Accept"), QByteArray(b"application/vnd.github+json"))
        request.setRawHeader(
            QByteArray(b"User-Agent"),
            QByteArray(f"claude-video-ingest/{current_version}".encode()),
        )
        # Stash the version on the request so we can compare on reply
        request.setAttribute(
            QNetworkRequest.Attribute.User, current_version
        )
        self._net.get(request)

    def _on_reply_finished(self, reply: QNetworkReply) -> None:
        """Parse the response. Silent on any failure — update checks are
        not important enough to interrupt the user."""
        try:
            current_version = reply.request().attribute(
                QNetworkRequest.Attribute.User
            )
            if reply.error() != QNetworkReply.NetworkError.NoError:
                logger.debug("Update check network error: %s", reply.errorString())
                return

            raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
            data = json.loads(raw)

            latest_tag: str = data.get("tag_name", "").lstrip("v")
            release_url: str = data.get("html_url", "")
            release_notes: str = (data.get("body") or "").strip()
            is_prerelease: bool = bool(data.get("prerelease", False))
            is_draft: bool = bool(data.get("draft", False))

            # Write the last-check timestamp regardless of result, so we
            # don't retry failed checks every launch.
            _write_last_check_time()

            if is_prerelease or is_draft:
                logger.debug("Latest release is prerelease/draft; skipping")
                return
            if not latest_tag or not _is_plain_semver(latest_tag):
                logger.debug("Latest tag not plain semver: %r", latest_tag)
                return

            if _semver_gt(latest_tag, current_version):
                info = UpdateInfo(
                    latest_tag=latest_tag,
                    current_version=current_version,
                    release_url=release_url,
                    release_notes=release_notes,
                )
                self.update_available.emit(info)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Update check parse error: %s", e)
        finally:
            reply.deleteLater()


# ---------------------------------------------------------------------------
# Version comparison — deliberately minimal, no external deps.
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _is_plain_semver(v: str) -> bool:
    """True if v is MAJOR.MINOR.PATCH with optional leading 'v'."""
    return bool(_SEMVER_RE.match(v))


def _semver_tuple(v: str) -> tuple[int, int, int]:
    m = _SEMVER_RE.match(v)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _semver_gt(latest: str, current: str) -> bool:
    """True if latest > current. Both assumed plain semver."""
    return _semver_tuple(latest) > _semver_tuple(current)


# ---------------------------------------------------------------------------
# Rate-limit cache: store last check timestamp in the app config dir.
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not base:
        base = str(Path.home() / ".config" / "Claude Video Ingest")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path / "update-check.json"


def _read_last_check_time() -> float | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("last_check_ts", 0)) or None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _write_last_check_time() -> None:
    path = _cache_path()
    try:
        path.write_text(json.dumps({"last_check_ts": time.time()}), encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write update-check cache: %s", e)
