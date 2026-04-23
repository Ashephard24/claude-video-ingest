"""
Settings dialog.

A modal dialog with editable fields (max frames, whisper model,
whisper fallback enable, library location) plus a read-only display
of the settings file path. Save persists to settings.json; Cancel
discards changes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..paths import library_root
from .settings import (
    WHISPER_MODEL_CHOICES,
    GuiSettings,
    load_settings,
    save_settings,
    settings_path,
)


def _is_writable_directory(path: Path) -> bool:
    """
    Return True only if `path` is an existing directory that we can write
    to. Tests writability concretely by creating and deleting a temp file
    — more reliable than os.access on Windows.
    """
    if not path.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path), prefix=".cvi-writetest-", delete=True
        ):
            pass
        return True
    except (OSError, PermissionError):
        return False


class SettingsDialog(QDialog):
    """
    Modal settings dialog. Call .exec() to show; check .result() for
    QDialog.DialogCode.Accepted if the user clicked Save.

    The dialog owns its own GuiSettings instance — it loads from disk
    on open and writes back on Save. The main window can query
    load_settings() again after the dialog closes.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        # Wider so the Library location field can show a typical absolute
        # path (e.g. C:\Users\<User>\Documents\claude-video-library) in
        # full, plus the Browse / Reset / Open buttons on its right.
        self.setMinimumWidth(720)

        self._settings = load_settings()
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        # --- Max frames ---
        self._max_frames_spin = QSpinBox()
        self._max_frames_spin.setRange(5, 300)
        self._max_frames_spin.setValue(self._settings.max_frames)
        self._max_frames_spin.setSuffix(" frames")
        self._max_frames_spin.setToolTip(
            "How many frames to extract per video. More frames = more context "
            "for Claude but more to upload. Default 60."
        )
        form.addRow("Max frames per video:", self._max_frames_spin)

        # --- Whisper model ---
        self._whisper_model_combo = QComboBox()
        for value, label in WHISPER_MODEL_CHOICES:
            self._whisper_model_combo.addItem(label, value)
        # Pre-select the saved model
        for i in range(self._whisper_model_combo.count()):
            if self._whisper_model_combo.itemData(i) == self._settings.whisper_model:
                self._whisper_model_combo.setCurrentIndex(i)
                break
        self._whisper_model_combo.setToolTip(
            "Model used when YouTube captions aren't available and "
            "Whisper falls back to local transcription. Larger = more "
            "accurate, much slower, larger download on first use."
        )
        form.addRow("Whisper model:", self._whisper_model_combo)

        # --- Whisper fallback enable ---
        self._whisper_enabled_check = QCheckBox(
            "Use Whisper as fallback when YouTube captions are unavailable"
        )
        self._whisper_enabled_check.setChecked(self._settings.use_whisper_fallback)
        self._whisper_enabled_check.setToolTip(
            "If unchecked, videos without YouTube captions will fail "
            "instead of falling back to Whisper transcription."
        )
        # Disabling whisper should also disable the model selector — it's
        # moot if the fallback itself is off.
        self._whisper_enabled_check.toggled.connect(
            self._whisper_model_combo.setEnabled
        )
        self._whisper_model_combo.setEnabled(self._settings.use_whisper_fallback)
        form.addRow("Transcript fallback:", self._whisper_enabled_check)

        root.addLayout(form)

        # --- Library location (editable) ---
        lib_label = QLabel("Library location:")
        lib_row_widget = QWidget()
        lib_value_row = QHBoxLayout(lib_row_widget)
        lib_value_row.setContentsMargins(0, 0, 0, 0)
        lib_value_row.setSpacing(6)
        # The QLineEdit always shows the effective library_root() path.
        # An empty GuiSettings.library_location means "use the platform
        # default"; we surface that default here so the user sees a real
        # path in the field.
        self._lib_path_edit = QLineEdit(str(library_root()))
        # Make sure the field can absorb extra horizontal room so a full
        # path is readable at a glance. Minimum width matters for small
        # default dialog widths; stretch=1 in the layout handles growth.
        self._lib_path_edit.setMinimumWidth(340)
        # Keep the tooltip synced with the current value so hovering
        # reveals the full path even if the field is clipped.
        self._lib_path_edit.setToolTip(self._lib_path_edit.text())
        self._lib_path_edit.textChanged.connect(self._lib_path_edit.setToolTip)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._on_browse_library_clicked)
        reset_btn = QPushButton("Reset to default")
        reset_btn.setToolTip("Clear the custom path and revert to the platform default.")
        reset_btn.clicked.connect(self._on_reset_library_clicked)
        open_lib_btn = QPushButton("Open")
        open_lib_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_root())))
        )
        lib_value_row.addWidget(self._lib_path_edit, stretch=1)
        lib_value_row.addWidget(browse_btn)
        lib_value_row.addWidget(reset_btn)
        lib_value_row.addWidget(open_lib_btn)
        form.addRow(lib_label, lib_row_widget)

        # --- Settings file location (read-only, informational) ---
        settings_loc_label = QLabel(
            f"<span style='color:#888;'>Settings file: "
            f"<code>{settings_path()}</code></span>"
        )
        settings_loc_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        settings_loc_label.setWordWrap(True)
        root.addWidget(settings_loc_label)

        # --- Save / Cancel ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_browse_library_clicked(self) -> None:
        """Open a directory picker starting at the current path."""
        start = self._lib_path_edit.text().strip() or str(library_root())
        if not Path(start).is_dir():
            start = str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose library location", start
        )
        if chosen:
            self._lib_path_edit.setText(chosen)

    def _on_reset_library_clicked(self) -> None:
        """Revert the field to the platform default library path."""
        # The default is computed by clearing the library_location
        # setting and re-resolving. We can't just call library_root()
        # here because the user may still have a different saved value.
        # Simpler: import the default-computing helper indirectly by
        # re-resolving with the current env var respected but settings
        # override ignored — but that requires plumbing. Practical
        # shortcut: show the user the default path directly.
        from ..paths import _settings_library_location
        import os as _os
        # If VIDEO_INGEST_LIBRARY is set, that's the effective default.
        env = _os.environ.get("VIDEO_INGEST_LIBRARY")
        if env:
            self._lib_path_edit.setText(str(Path(env).expanduser().resolve()))
            return
        # Otherwise fall all the way back to the hardcoded default.
        self._lib_path_edit.setText(
            str(Path.home() / "Documents" / "claude-video-library")
        )

    def _on_save(self) -> None:
        """Persist the form state and close with Accepted."""
        raw_path = self._lib_path_edit.text().strip()
        new_lib_path = Path(raw_path).expanduser() if raw_path else None
        current_effective = library_root()

        # Decide what to persist in settings.library_location. If the
        # user's path equals the platform default (no env-var override,
        # no saved override), store empty string — "use default".
        default_path = Path.home() / "Documents" / "claude-video-library"
        env_override = os.environ.get("VIDEO_INGEST_LIBRARY")
        platform_default = (
            Path(env_override).expanduser().resolve() if env_override else default_path
        )
        if new_lib_path is None or new_lib_path.resolve() == platform_default.resolve():
            library_location_value = ""
            effective_new_path = platform_default
        else:
            library_location_value = str(new_lib_path.resolve())
            effective_new_path = new_lib_path.resolve()

        # Validate the target path only if it's actually changing.
        changing = effective_new_path.resolve() != current_effective.resolve()
        if changing:
            if not effective_new_path.exists():
                QMessageBox.warning(
                    self,
                    "Library location invalid",
                    f"The path does not exist:\n\n{effective_new_path}\n\n"
                    f"Create the folder first, or choose an existing folder.",
                )
                return
            if not _is_writable_directory(effective_new_path):
                QMessageBox.warning(
                    self,
                    "Library location not writable",
                    f"This path isn't writable or isn't a directory:\n\n"
                    f"{effective_new_path}\n\n"
                    f"Pick a location where you have write access.",
                )
                return

            # Warn-and-punt: we do NOT auto-migrate existing videos.
            warn = QMessageBox(self)
            warn.setWindowTitle("Library location change")
            warn.setIcon(QMessageBox.Icon.Warning)
            warn.setText(
                f"You're changing the library location to:\n\n"
                f"    {effective_new_path}\n\n"
                f"Videos already in your current library will NOT be moved "
                f"automatically. If you want to keep them, close this dialog, "
                f"move the existing folders to the new location manually, "
                f"then come back and change the setting."
            )
            warn.setInformativeText("Proceed with the change?")
            change_btn = warn.addButton(
                "Change location", QMessageBox.ButtonRole.AcceptRole
            )
            cancel_btn = warn.addButton(QMessageBox.StandardButton.Cancel)
            warn.setDefaultButton(cancel_btn)
            warn.exec()
            if warn.clickedButton() is not change_btn:
                return

        updated = GuiSettings(
            max_frames=self._max_frames_spin.value(),
            whisper_model=self._whisper_model_combo.currentData(),
            use_whisper_fallback=self._whisper_enabled_check.isChecked(),
            library_location=library_location_value,
        )
        save_settings(updated)
        self.accept()
