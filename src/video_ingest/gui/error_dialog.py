"""
Error dialog.

Shows a failed ingest with the `what` message prominent, the `fix`
below, and two action buttons:
  - "Copy error details" — copies a markdown-formatted bug report
    to clipboard (URL, timestamp, what, fix, log file path).
  - "Open log file" — opens the error log in the system editor.
    Only enabled when the log file exists.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from .. import __version__
from ..paths import error_log_path


class ErrorDialog(QDialog):
    """
    Modal error dialog.

    Args:
        url: the video URL that was being processed
        what: short description of what went wrong (from VideoIngestError.what
              or a generic "Unexpected error: ..." string)
        fix:  multi-line string describing what the user can do. For classified
              errors this is `\\n`.join(VideoIngestError.fix). For unexpected
              errors it points at the log file.

    Usage:
        dlg = ErrorDialog(url, what, fix, parent=main_window)
        dlg.exec()
    """

    def __init__(
        self,
        url: str,
        what: str,
        fix: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._what = what
        self._fix = fix

        self.setWindowTitle("Ingest failed")
        self.setMinimumSize(560, 360)

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        # What went wrong — prominent
        what_label = QLabel(self._what)
        what_label.setWordWrap(True)
        what_label.setStyleSheet("font-weight: bold; font-size: 13pt;")
        root.addWidget(what_label)

        # URL for context
        url_label = QLabel(f"<span style='color:#666;'>URL: {self._url}</span>")
        url_label.setTextInteractionFlags(
            url_label.textInteractionFlags()
            | url_label.textInteractionFlags().TextSelectableByMouse
        )
        url_label.setWordWrap(True)
        root.addWidget(url_label)

        # Fix / next steps
        if self._fix:
            fix_label = QLabel("What to do:")
            fix_label.setStyleSheet("font-weight: bold;")
            root.addWidget(fix_label)

            fix_box = QTextEdit()
            fix_box.setReadOnly(True)
            fix_box.setPlainText(self._fix)
            fix_box.setMaximumHeight(140)
            root.addWidget(fix_box)

        # Action row: Copy details, Open log, Close
        action_row = QHBoxLayout()

        copy_btn = QPushButton("Copy error details")
        copy_btn.setToolTip(
            "Copy a markdown-formatted bug report to your clipboard, "
            "ready to paste into a Claude chat or a bug report."
        )
        copy_btn.clicked.connect(self._on_copy_clicked)

        log_path = error_log_path()
        self._open_log_btn = QPushButton("Open log file")
        self._open_log_btn.setToolTip(
            "Open the full error log in your system's default editor."
        )
        self._open_log_btn.setEnabled(log_path.exists())
        self._open_log_btn.clicked.connect(self._on_open_log_clicked)

        action_row.addWidget(copy_btn)
        action_row.addWidget(self._open_log_btn)
        action_row.addStretch()

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        action_row.addWidget(close_buttons)

        root.addLayout(action_row)

        # Feedback label (shows "Copied!" briefly after Copy is clicked)
        self._feedback_label = QLabel("")
        self._feedback_label.setStyleSheet("color: #2a7;")
        root.addWidget(self._feedback_label)

    def _on_copy_clicked(self) -> None:
        """Build a markdown report and copy to clipboard."""
        report = self._build_report()
        QGuiApplication.clipboard().setText(report)
        self._feedback_label.setText("Copied to clipboard.")

    def _on_open_log_clicked(self) -> None:
        log_path = error_log_path()
        if log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))

    def _build_report(self) -> str:
        """Markdown bug report — pasteable into a Claude chat or issue."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path = error_log_path()
        log_line = (
            f"- Error log: `{log_path}` (exists)"
            if log_path.exists()
            else "- Error log: not written (this was a classified error)"
        )
        return (
            f"# Claude Video Ingest — error report\n\n"
            f"- **Version**: {__version__}\n"
            f"- **Timestamp**: {timestamp}\n"
            f"- **URL**: {self._url}\n"
            f"{log_line}\n\n"
            f"## What went wrong\n\n"
            f"{self._what}\n\n"
            f"## Suggested fix / context\n\n"
            f"{self._fix or '(none)'}\n"
        )
