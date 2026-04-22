"""
Settings dialog.

A modal dialog with three editable fields (max frames, whisper model,
whisper fallback enable) plus a read-only library location display.
Save persists to settings.json; Cancel discards changes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..paths import library_root
from .settings import (
    WHISPER_MODEL_CHOICES,
    GuiSettings,
    load_settings,
    save_settings,
    settings_path,
)


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
        self.setMinimumWidth(480)

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

        # --- Library location (read-only, with "Open" button) ---
        lib_label = QLabel("Library location:")
        lib_value_row = QHBoxLayout()
        self._lib_path_label = QLabel(str(library_root()))
        self._lib_path_label.setStyleSheet("color: #555;")
        self._lib_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        open_lib_btn = QPushButton("Open")
        open_lib_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_root())))
        )
        lib_value_row.addWidget(self._lib_path_label, stretch=1)
        lib_value_row.addWidget(open_lib_btn)
        form.addRow(lib_label, lib_value_row)

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

    def _on_save(self) -> None:
        """Persist the form state and close with Accepted."""
        updated = GuiSettings(
            max_frames=self._max_frames_spin.value(),
            whisper_model=self._whisper_model_combo.currentData(),
            use_whisper_fallback=self._whisper_enabled_check.isChecked(),
        )
        save_settings(updated)
        self.accept()
