"""
GUI entry point and main window for Claude Video Ingest.

Architecture:
    gui_main()      — entry point. Creates QApplication and MainWindow.
    MainWindow      — the window. URL input + queue + controls + log.
    QueueItem       — the data for one job in the queue.
    QueueItemWidget — the visual row for one job (status + URL + ✕ button).
    IngestWorker    — QObject that runs pipeline.ingest() on a QThread.
    QtProgress      — pipeline.Progress impl that emits Qt signals.

The GUI is a thin layer over the existing pipeline. All real work happens
in video_ingest.pipeline; this module drives it from a window instead of a
terminal and keeps the UI thread free while the pipeline runs.

Queue model (milestone 4):
    - Start button kicks off sequential processing. The queue auto-advances
      to the next PENDING item when the current one finishes or fails.
    - Stop button (replaces Start while running) requests a graceful halt.
      The current item completes its CURRENT STEP, then raises IngestCancelled
      at the next step boundary. Steps are atomic — mid-Whisper cancellation
      is NOT supported in v1 (see docstring on CancelToken in pipeline.py).
    - Each item has a ✕ button. On a pending item: removes it. On the
      running item: requests cancellation AND removes it when it halts.
    - Items are processed one at a time (parallel ffmpeg + Whisper would
      fight for CPU — worse UX than sequential).
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QRegularExpression,
    QSortFilterProxyModel,
    QThread,
    Qt,
    QMimeData,
    QModelIndex,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QDrag, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from .. import __version__
from ..doctor import run_checks
from ..errors import IngestCancelled, VideoIngestError
from ..library import LibraryEntry, read_library_index, reconcile_library_index
from ..paths import library_root
from ..pipeline import CancelToken, Progress, ingest
from ..utils import parse_youtube_url
from .settings import load_settings
from .settings_dialog import SettingsDialog
from .error_dialog import ErrorDialog
from .update_checker import UpdateChecker, UpdateInfo


# ---------------------------------------------------------------------------
# Queue data model
# ---------------------------------------------------------------------------

class QueueStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def symbol(self) -> str:
        return {
            QueueStatus.PENDING: "⏳",
            QueueStatus.RUNNING: "▶",
            QueueStatus.DONE: "✓",
            QueueStatus.FAILED: "✗",
            QueueStatus.CANCELLED: "⊘",
        }[self]


@dataclass
class QueueItem:
    """A single job in the queue. One-to-one with a QueueItemWidget row."""
    url: str
    status: QueueStatus = QueueStatus.PENDING
    result_folder: Path | None = None
    error_what: str = ""
    error_fix: str = ""


# ---------------------------------------------------------------------------
# Progress bridge: pipeline -> Qt signals
# ---------------------------------------------------------------------------

class QtProgress(QObject, Progress):
    """
    Progress reporter that emits Qt signals instead of printing to a terminal.

    Lives on the worker thread. Signals are connected to slots on the main
    window with Qt.QueuedConnection (the default across threads), so UI
    updates happen safely on the GUI thread.

    Inherits QObject first so Qt's meta-object system picks up the signals.
    Progress is a plain Python base class — no MRO conflict.
    """

    step_changed = Signal(int, int, str)          # current, total, label
    substep_logged = Signal(str)                  # label
    ok_logged = Signal(str)                       # message
    warn_logged = Signal(str)                     # message
    frame_progress_changed = Signal(int, int)     # n, total

    def __init__(self) -> None:
        super().__init__()

    def step(self, current: int, total: int, label: str) -> None:
        self.step_changed.emit(current, total, label)

    def substep(self, label: str) -> None:
        self.substep_logged.emit(label)

    def ok(self, message: str) -> None:
        self.ok_logged.emit(message)

    def warn(self, message: str) -> None:
        self.warn_logged.emit(message)

    def frame_progress(self, n: int, total: int) -> None:
        self.frame_progress_changed.emit(n, total)


# ---------------------------------------------------------------------------
# Worker: runs pipeline.ingest() on a background thread
# ---------------------------------------------------------------------------

@dataclass
class IngestSettings:
    """Snapshot of settings for a single ingest run. Milestone 7 will
    wire a real Settings screen to populate this; for now we pass the
    1.2.2 CLI defaults."""
    url: str
    use_whisper_fallback: bool = True
    whisper_model: str = "base"
    max_frames: int = 60
    min_frame_interval: float = 30.0
    scene_threshold: float = 0.35
    batch_size: int = 18


class IngestWorker(QObject):
    """
    Runs a single ingest on its own QThread. Owns a CancelToken that the
    main thread can flip to request cooperative cancellation.

    Lifecycle:
        - MainWindow creates an IngestWorker and a QThread.
        - MainWindow moves the worker to the thread.
        - thread.started -> worker.run
        - worker emits finished_item / failed_item / cancelled_item at the end
        - MainWindow tears down the thread on any of those signals.
    """

    started_item = Signal(str)                # url
    finished_item = Signal(str, str)          # url, folder_path
    failed_item = Signal(str, str, str)       # url, what, fix_text
    cancelled_item = Signal(str)              # url
    progress = Signal(object)                 # QtProgress instance

    def __init__(self, settings: IngestSettings) -> None:
        super().__init__()
        self._settings = settings
        self._cancel_token = CancelToken()

    def request_cancel(self) -> None:
        """
        Called from the main thread (safe — CancelToken uses a single
        boolean). Takes effect at the pipeline's next step boundary.
        """
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        self.started_item.emit(self._settings.url)

        # Create QtProgress on the worker thread so its signals originate
        # there; they cross to the main thread via QueuedConnection.
        progress = QtProgress()
        self.progress.emit(progress)

        try:
            folder: Path = ingest(
                self._settings.url,
                use_whisper_fallback=self._settings.use_whisper_fallback,
                whisper_model=self._settings.whisper_model,
                max_frames=self._settings.max_frames,
                min_frame_interval=self._settings.min_frame_interval,
                scene_threshold=self._settings.scene_threshold,
                batch_size=self._settings.batch_size,
                progress=progress,
                cancel_token=self._cancel_token,
            )
        except IngestCancelled:
            self.cancelled_item.emit(self._settings.url)
            return
        except VideoIngestError as e:
            fix_text = "\n".join(e.fix) if e.fix else ""
            self.failed_item.emit(self._settings.url, e.what, fix_text)
            return
        except Exception as e:  # noqa: BLE001
            # Unexpected error: dump full traceback to the error log and
            # tell the caller where to find it. The GUI's ErrorDialog will
            # offer the user "Open log file" and "Copy error details".
            from ..cli import write_error_log
            log_path = write_error_log(e)
            self.failed_item.emit(
                self._settings.url,
                f"Unexpected error: {e}",
                f"Full error written to:\n{log_path}\n\n"
                f"Try again — many errors are transient. If it keeps "
                f"failing, open the log file and paste it into a Claude chat.",
            )
            return

        self.finished_item.emit(self._settings.url, str(folder))


# ---------------------------------------------------------------------------
# Queue row widget: status + URL + per-item ✕ button
# ---------------------------------------------------------------------------

class QueueItemWidget(QWidget):
    """
    A single row in the queue list. Shows status symbol, URL, and an ✕
    button. The ✕ emits `removal_requested(url)` — the main window decides
    what to do based on the item's current status (pending → drop it;
    running → cancel it; done/failed/cancelled → clear it).
    """

    removal_requested = Signal(str)  # url

    def __init__(self, item: QueueItem) -> None:
        super().__init__()
        self._item = item

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._status_label = QLabel(item.status.symbol)
        self._status_label.setFixedWidth(20)
        self._url_label = QLabel(item.url)
        self._url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._url_label.setWordWrap(False)
        self._url_label.setSizePolicy(
            self._url_label.sizePolicy().horizontalPolicy(),
            self._url_label.sizePolicy().verticalPolicy(),
        )

        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedWidth(28)
        self._remove_btn.setToolTip("Remove from queue (or cancel if running)")
        self._remove_btn.clicked.connect(
            lambda: self.removal_requested.emit(self._item.url)
        )

        layout.addWidget(self._status_label)
        layout.addWidget(self._url_label, stretch=1)
        layout.addWidget(self._remove_btn)

    def refresh(self) -> None:
        """Re-render from the backing QueueItem's current state."""
        self._status_label.setText(self._item.status.symbol)
        # Done/failed/cancelled items don't need a cancel affordance;
        # the ✕ becomes a "remove from list" button for them.
        if self._item.status in (
            QueueStatus.DONE,
            QueueStatus.FAILED,
            QueueStatus.CANCELLED,
        ):
            self._remove_btn.setToolTip("Remove from list")
        elif self._item.status == QueueStatus.RUNNING:
            self._remove_btn.setToolTip("Cancel (takes effect after current step)")
        else:
            self._remove_btn.setToolTip("Remove from queue")


# ---------------------------------------------------------------------------
# Library view: browse ingested videos and their contents
# ---------------------------------------------------------------------------

class DraggableTreeWidget(QTreeWidget):
    """
    QTreeWidget that emits drag-outs with `text/uri-list` MIME data pointing
    at real filesystem paths. Browsers (including Claude.ai) treat this as
    a file drop, the same as if the user had dragged from Explorer / Finder.

    Supports multi-selection drags: users can Ctrl/Cmd-click several files
    or folders in the tree and drag them all out together.

    Cross-platform notes:
      - Windows: Qt automatically adds CF_HDROP alongside text/uri-list.
      - macOS: file:// URLs translate to NSFilenamesPboardType.
      - Linux (GTK apps including browsers): text/uri-list is the primary
        format. No extra handling needed.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        # Drag-only: the tree doesn't accept drops. Prevents Qt from
        # treating dropped files as an internal move.
        self.setDragDropMode(QTreeWidget.DragDropMode.DragOnly)

    def startDrag(self, supported_actions) -> None:  # type: ignore[override]
        """
        Build a QDrag with the selected items' filesystem paths as URLs.
        Called automatically by Qt when the user initiates a drag from
        a selected row.
        """
        paths = self._selected_paths()
        if not paths:
            return

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        # Also expose plain-text paths as a fallback — useful if the target
        # only reads text/plain. Browsers prefer text/uri-list so this is
        # belt-and-suspenders.
        mime.setText("\n".join(str(p) for p in paths))

        drag = QDrag(self)
        drag.setMimeData(mime)
        # Qt picks a reasonable default cursor; we don't override it.
        drag.exec(Qt.DropAction.CopyAction)

    def _selected_paths(self) -> list[Path]:
        """Collect filesystem paths from the UserRole data on selected rows."""
        paths: list[Path] = []
        for qt_item in self.selectedItems():
            path_str = qt_item.data(0, Qt.ItemDataRole.UserRole)
            if path_str:
                p = Path(path_str)
                if p.exists():
                    paths.append(p)
        return paths


_LIB_COL_TITLE = 0
_LIB_COL_CREATOR = 1
_LIB_COL_DURATION = 2
_LIB_COL_INGESTED = 3
_LIB_ROLE_FOLDER_NAME = Qt.ItemDataRole.UserRole
_LIB_ROLE_DURATION_SECONDS = Qt.ItemDataRole.UserRole + 1


class _LibraryProxyModel(QSortFilterProxyModel):
    """Filters on all columns; sorts Duration numerically by raw seconds."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # type: ignore[override]
        if left.column() == _LIB_COL_DURATION and right.column() == _LIB_COL_DURATION:
            l = left.data(_LIB_ROLE_DURATION_SECONDS)
            r = right.data(_LIB_ROLE_DURATION_SECONDS)
            if isinstance(l, int) and isinstance(r, int):
                return l < r
        return super().lessThan(left, right)


class LibraryView(QWidget):
    """
    Second tab. Master-detail layout:
        Left  — QTableView of LibraryEntry rows (sortable by column,
                filterable via search box), newest first by default.
        Right — QTreeWidget of the selected folder's contents.

    Data source: read_library_index() from ../library.py, which reads the
    JSON sidecar (library.json) with a disk-walk fallback. We call this
    on refresh() — which fires on tab switch and after each ingest.
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[LibraryEntry] = []
        self._selected_folder: Path | None = None
        self._build_ui()

    # ------------- UI construction -------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header row: label + refresh button + open-library-folder button
        header_row = QHBoxLayout()
        header_label = QLabel("Library")
        header_label.setFont(_bold(header_label.font()))
        self._empty_label = QLabel("")
        self._empty_label.setStyleSheet("color: #888;")

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)

        self._open_root_btn = QPushButton("Open library folder")
        self._open_root_btn.setToolTip(
            "Open the root library folder in your system file manager."
        )
        self._open_root_btn.clicked.connect(self._on_open_root_clicked)

        header_row.addWidget(header_label)
        header_row.addWidget(self._empty_label, stretch=1)
        header_row.addWidget(self._refresh_btn)
        header_row.addWidget(self._open_root_btn)
        root.addLayout(header_row)

        # Master-detail splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left pane: search + sortable table of videos
        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("Filter by title or creator...")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._on_filter_changed)
        left_layout.addWidget(self._filter_input)

        self._videos_model = QStandardItemModel(0, 4, self)
        self._videos_model.setHorizontalHeaderLabels(
            ["Title", "Creator", "Duration", "Ingested"]
        )
        self._videos_proxy = _LibraryProxyModel(self)
        self._videos_proxy.setSourceModel(self._videos_model)
        self._videos_proxy.setFilterKeyColumn(-1)  # filter across all columns
        self._videos_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self._videos_table = QTableView()
        self._videos_table.setModel(self._videos_proxy)
        self._videos_table.setSortingEnabled(True)
        self._videos_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._videos_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._videos_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._videos_table.verticalHeader().setVisible(False)
        # Taller default rows so wrapped titles don't clip.
        self._videos_table.verticalHeader().setDefaultSectionSize(32)
        self._videos_table.setWordWrap(True)
        header = self._videos_table.horizontalHeader()
        header.setSectionResizeMode(_LIB_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            _LIB_COL_CREATOR, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            _LIB_COL_DURATION, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            _LIB_COL_INGESTED, QHeaderView.ResizeMode.ResizeToContents
        )
        # Default sort: newest first
        self._videos_table.sortByColumn(_LIB_COL_INGESTED, Qt.SortOrder.DescendingOrder)
        self._videos_table.selectionModel().currentRowChanged.connect(
            self._on_table_current_row_changed
        )
        left_layout.addWidget(self._videos_table, stretch=1)

        splitter.addWidget(left_pane)

        # Right pane: contents of selected video folder
        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self._detail_title = QLabel("Select a video to see its contents")
        self._detail_title.setFont(_bold(self._detail_title.font()))
        self._detail_title.setWordWrap(True)
        right_layout.addWidget(self._detail_title)

        self._detail_subtitle = QLabel("")
        self._detail_subtitle.setStyleSheet("color: #666;")
        self._detail_subtitle.setWordWrap(True)
        right_layout.addWidget(self._detail_subtitle)

        self._youtube_link_label = QLabel()
        self._youtube_link_label.setOpenExternalLinks(True)
        self._youtube_link_label.setTextFormat(Qt.TextFormat.RichText)
        self._youtube_link_label.setVisible(False)
        right_layout.addWidget(self._youtube_link_label)

        # Tree of files/folders in the video folder. DraggableTreeWidget
        # lets the user drag items out of the app window into a Claude chat.
        self._contents_tree = DraggableTreeWidget()
        self._contents_tree.setHeaderLabels(["Name", "Size"])
        self._contents_tree.setRootIsDecorated(True)
        self._contents_tree.setUniformRowHeights(True)
        right_layout.addWidget(self._contents_tree, stretch=1)

        # Action row: open folder + delete + drag hint
        action_row = QHBoxLayout()
        self._open_video_folder_btn = QPushButton("Open this folder")
        self._open_video_folder_btn.setEnabled(False)
        self._open_video_folder_btn.clicked.connect(
            self._on_open_video_folder_clicked
        )
        self._delete_video_btn = QPushButton("Delete")
        self._delete_video_btn.setEnabled(False)
        self._delete_video_btn.setToolTip(
            "Move this video's folder to the OS recycle bin / trash."
        )
        self._delete_video_btn.clicked.connect(self._on_delete_clicked)
        drag_hint = QLabel(
            "Drag START-HERE into Claude first, then batch-1, batch-2, ..."
        )
        drag_hint.setStyleSheet("color: #666; font-style: italic;")
        action_row.addWidget(self._open_video_folder_btn)
        action_row.addWidget(self._delete_video_btn)
        action_row.addStretch()
        action_row.addWidget(drag_hint)
        right_layout.addLayout(action_row)

        splitter.addWidget(right_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([280, 540])
        root.addWidget(splitter, stretch=1)

    # ------------- Public API -------------

    def refresh(self) -> None:
        """Reload the entries list from disk and repopulate the left pane."""
        # Save column widths before the model rebuild so auto-sized columns
        # don't momentarily collapse while rows are swapped out. The Stretch
        # column ignores setColumnWidth and refills automatically.
        saved_widths = {
            col: self._videos_table.columnWidth(col)
            for col in range(self._videos_model.columnCount())
        }

        try:
            self._entries = read_library_index()
        except Exception as e:  # noqa: BLE001
            # We never want a library-read error to crash the GUI — log
            # to the empty-label and show no entries.
            self._entries = []
            self._empty_label.setText(f"Could not read library: {e}")
            self._videos_model.removeRows(0, self._videos_model.rowCount())
            return

        # Remember selection so we can restore it after repopulating.
        previously_selected = self._selected_folder

        self._videos_model.removeRows(0, self._videos_model.rowCount())

        if not self._entries:
            self._empty_label.setText(
                "No videos yet. Ingest one on the Queue tab."
            )
            self._detail_title.setText("Select a video to see its contents")
            self._detail_subtitle.setText("")
            self._youtube_link_label.setVisible(False)
            self._contents_tree.clear()
            self._open_video_folder_btn.setEnabled(False)
            self._delete_video_btn.setEnabled(False)
            return

        self._empty_label.setText(f"{len(self._entries)} video(s)")

        for entry in self._entries:
            title_item = QStandardItem(entry.title)
            title_item.setData(entry.folder_name, _LIB_ROLE_FOLDER_NAME)
            creator_item = QStandardItem(entry.creator)
            duration_item = QStandardItem(entry.duration)
            duration_item.setData(
                _parse_duration_to_seconds(entry.duration),
                _LIB_ROLE_DURATION_SECONDS,
            )
            ingested_item = QStandardItem(entry.ingest_date)
            self._videos_model.appendRow(
                [title_item, creator_item, duration_item, ingested_item]
            )

        # Restore selection by folder_name, or default to first visible row.
        target_row_source = None
        if previously_selected is not None:
            for i, entry in enumerate(self._entries):
                if library_root() / entry.folder_name == previously_selected:
                    target_row_source = i
                    break

        # Restore prior column widths where we had them. The Stretch column
        # is re-filled by Qt; these calls are no-ops for it.
        for col, width in saved_widths.items():
            if width > 0:
                self._videos_table.setColumnWidth(col, width)

        if target_row_source is not None:
            src_idx = self._videos_model.index(target_row_source, _LIB_COL_TITLE)
            proxy_idx = self._videos_proxy.mapFromSource(src_idx)
            if proxy_idx.isValid():
                self._videos_table.selectionModel().setCurrentIndex(
                    proxy_idx,
                    self._videos_table.selectionModel().SelectionFlag.ClearAndSelect
                    | self._videos_table.selectionModel().SelectionFlag.Rows,
                )
                return

        if self._videos_proxy.rowCount() > 0:
            first = self._videos_proxy.index(0, _LIB_COL_TITLE)
            self._videos_table.selectionModel().setCurrentIndex(
                first,
                self._videos_table.selectionModel().SelectionFlag.ClearAndSelect
                | self._videos_table.selectionModel().SelectionFlag.Rows,
            )

    # ------------- Event handlers -------------

    @Slot(QModelIndex, QModelIndex)
    def _on_table_current_row_changed(
        self, current: QModelIndex, _previous: QModelIndex
    ) -> None:
        if not current.isValid():
            self._selected_folder = None
            self._contents_tree.clear()
            self._detail_title.setText("Select a video to see its contents")
            self._detail_subtitle.setText("")
            self._youtube_link_label.setVisible(False)
            self._open_video_folder_btn.setEnabled(False)
            self._delete_video_btn.setEnabled(False)
            return

        src_idx = self._videos_proxy.mapToSource(current)
        title_idx = self._videos_model.index(src_idx.row(), _LIB_COL_TITLE)
        folder_name = self._videos_model.data(title_idx, _LIB_ROLE_FOLDER_NAME)
        entry = next((e for e in self._entries if e.folder_name == folder_name), None)
        if entry is None:
            self._selected_folder = None
            self._contents_tree.clear()
            self._youtube_link_label.setVisible(False)
            self._open_video_folder_btn.setEnabled(False)
            self._delete_video_btn.setEnabled(False)
            return

        folder = library_root() / entry.folder_name
        self._selected_folder = folder

        self._detail_title.setText(entry.title)
        self._detail_subtitle.setText(
            f"by {entry.creator} • {entry.duration} • ingested {entry.ingest_date}"
        )
        if entry.video_url:
            self._youtube_link_label.setText(
                f'<a href="{entry.video_url}">Watch on YouTube ↗</a>'
            )
            self._youtube_link_label.setVisible(True)
        else:
            self._youtube_link_label.setVisible(False)
        self._populate_contents_tree(folder)
        self._open_video_folder_btn.setEnabled(folder.exists())
        self._delete_video_btn.setEnabled(folder.exists())

    @Slot()
    def _on_delete_clicked(self) -> None:
        if self._selected_folder is None:
            return
        folder_name = self._selected_folder.name
        box = QMessageBox(self)
        box.setWindowTitle("Delete video")
        box.setText(
            f"Move '{folder_name}' to the recycle bin?\n\n"
            f"You can recover it from your recycle bin / trash if needed."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        from ..library import delete_video_folder
        try:
            delete_video_folder(folder_name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Delete failed", str(e))
            return
        self.refresh()

    @Slot(str)
    def _on_filter_changed(self, text: str) -> None:
        # Escape regex metacharacters in the user's literal input.
        pattern = QRegularExpression.escape(text)
        self._videos_proxy.setFilterRegularExpression(
            QRegularExpression(
                pattern,
                QRegularExpression.PatternOption.CaseInsensitiveOption,
            )
        )

    @Slot()
    def _on_open_root_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(library_root())))

    @Slot()
    def _on_open_video_folder_clicked(self) -> None:
        if self._selected_folder is not None and self._selected_folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected_folder)))

    # ------------- Tree population -------------

    def _populate_contents_tree(self, folder: Path) -> None:
        self._contents_tree.clear()
        if not folder.exists():
            placeholder = QTreeWidgetItem(["(folder no longer exists on disk)", ""])
            self._contents_tree.addTopLevelItem(placeholder)
            return

        # Drag priority: START-HERE file first, then batch folders in
        # order, then everything else. Matches the user workflow: drag
        # START-HERE into Claude, then drag batch-1/, batch-2/, ...
        children = list(folder.iterdir())
        start_here = [p for p in children if p.name.startswith("START-HERE")]
        batches = sorted(
            [p for p in children if p.is_dir() and p.name.startswith("batch-")],
            key=lambda p: _batch_sort_key(p.name),
        )
        others = sorted(
            [
                p
                for p in children
                if p not in start_here and p not in batches
            ],
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
        ordered = start_here + batches + others

        for path in ordered:
            item = _build_tree_item(path)
            self._contents_tree.addTopLevelItem(item)

        # Size columns reasonably
        self._contents_tree.resizeColumnToContents(0)
        self._contents_tree.resizeColumnToContents(1)


# ---------------------------------------------------------------------------
# Helpers for LibraryView
# ---------------------------------------------------------------------------

def _batch_sort_key(name: str) -> tuple[int, str]:
    """Sort batch-1, batch-2, ..., batch-10 correctly (numeric, not lex)."""
    try:
        n = int(name.split("-", 1)[1])
        return (n, name)
    except (IndexError, ValueError):
        return (10**9, name)


def _parse_duration_to_seconds(text: str) -> int:
    """Parse a duration string like '1:02:05' or '2:05' into seconds."""
    parts = text.strip().split(":")
    try:
        ints = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(ints) == 3:
        h, m, s = ints
        return h * 3600 + m * 60 + s
    if len(ints) == 2:
        m, s = ints
        return m * 60 + s
    if len(ints) == 1:
        return ints[0]
    return 0


def _build_tree_item(path: Path) -> QTreeWidgetItem:
    """Recursively build a QTreeWidgetItem for a path."""
    if path.is_dir():
        item = QTreeWidgetItem([path.name + "/", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            item.addChild(_build_tree_item(child))
        return item
    else:
        size_str = _format_file_size(path.stat().st_size) if path.exists() else ""
        item = QTreeWidgetItem([path.name, size_str])
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))
        return item


def _format_file_size(n: int) -> str:
    """Human-friendly byte count."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Claude Video Ingest {__version__}")
        self.resize(820, 680)

        # Queue state. `_items` is the source of truth; `_queue_list` mirrors
        # it visually. Parallel arrays — index i in _items corresponds to the
        # QListWidgetItem at row i in _queue_list.
        self._items: list[QueueItem] = []

        # Active worker state. Only one job runs at a time (sequential).
        self._thread: QThread | None = None
        self._worker: IngestWorker | None = None
        self._running_item: QueueItem | None = None

        # When True, auto-advance to the next pending item after each completion.
        # Flipped off by Stop button or when the queue drains.
        self._processing = False

        self._build_ui()

    # ------------- UI construction -------------

    def _build_ui(self) -> None:
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self.setCentralWidget(central)

        # Update banner — hidden until an update is actually available.
        self._update_banner = self._build_update_banner()
        self._update_banner.setVisible(False)
        central_layout.addWidget(self._update_banner)

        self._tabs = QTabWidget()
        central_layout.addWidget(self._tabs, stretch=1)

        queue_tab = self._build_queue_tab()
        self._tabs.addTab(queue_tab, "Queue")

        self._library_view = LibraryView()
        self._tabs.addTab(self._library_view, "Library")

        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._build_menu_bar()

        # Kick off the async update check. Silent on failure; shows the
        # banner if a newer release is found.
        self._update_checker = UpdateChecker(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.check(__version__)

    def _build_update_banner(self) -> QWidget:
        """A dismissible 'update available' strip that sits above the tabs."""
        banner = QWidget()
        banner.setStyleSheet(
            "background-color: #fff6d9; border-bottom: 1px solid #e0d080;"
        )
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 8, 8, 8)
        layout.setSpacing(10)

        self._update_banner_label = QLabel("An update is available.")
        self._update_banner_label.setStyleSheet("color: #6a4a00;")
        self._update_banner_label.setWordWrap(True)

        self._update_banner_download_btn = QPushButton("Download")
        self._update_banner_download_btn.clicked.connect(
            self._on_update_download_clicked
        )
        self._update_banner_dismiss_btn = QPushButton("Dismiss")
        self._update_banner_dismiss_btn.clicked.connect(
            lambda: self._update_banner.setVisible(False)
        )

        layout.addWidget(self._update_banner_label, stretch=1)
        layout.addWidget(self._update_banner_download_btn)
        layout.addWidget(self._update_banner_dismiss_btn)
        return banner

    @Slot(object)
    def _on_update_available(self, info: UpdateInfo) -> None:
        """Show the update banner with the release info."""
        self._update_banner_url = info.release_url
        self._update_banner_label.setText(
            f"Version {info.latest_tag} is available "
            f"(you have {info.current_version})."
        )
        self._update_banner.setVisible(True)

    @Slot()
    def _on_update_download_clicked(self) -> None:
        url = getattr(self, "_update_banner_url", None)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _build_menu_bar(self) -> None:
        """Menu bar: File → Settings, Quit; Tools → Doctor, Reconcile; Help → About."""
        menubar = self.menuBar()

        # Hold Python-side references to the QMenu objects. Without these,
        # PySide6 (6.11+) garbage-collects the menus after this method
        # returns even though the menubar holds them in C++, which leaves
        # the menu titles visible but unclickable.
        self._file_menu = menubar.addMenu("&File")
        settings_action = self._file_menu.addAction("&Settings...")
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._on_settings_clicked)
        self._file_menu.addSeparator()
        quit_action = self._file_menu.addAction("&Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)

        self._tools_menu = menubar.addMenu("&Tools")
        doctor_action = self._tools_menu.addAction("Run &Doctor (environment check)")
        doctor_action.triggered.connect(self._on_doctor_clicked)
        reconcile_action = self._tools_menu.addAction("&Reconcile library (prune deleted folders)")
        reconcile_action.triggered.connect(self._on_reconcile_clicked)

        self._help_menu = menubar.addMenu("&Help")
        about_action = self._help_menu.addAction("&About Claude Video Ingest")
        about_action.triggered.connect(self._on_about_clicked)

    def _build_queue_tab(self) -> QWidget:
        """Build the Queue tab's contents (formerly the whole window)."""
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Row: URL input + Add button
        url_row = QHBoxLayout()
        url_label = QLabel("YouTube URL:")
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self._url_input.returnPressed.connect(self._on_add_clicked)
        self._add_btn = QPushButton("Add to Queue")
        self._add_btn.clicked.connect(self._on_add_clicked)
        url_row.addWidget(url_label)
        url_row.addWidget(self._url_input, stretch=1)
        url_row.addWidget(self._add_btn)
        root.addLayout(url_row)

        # Queue list header
        queue_header_row = QHBoxLayout()
        queue_label = QLabel("Queue")
        queue_label.setFont(_bold(queue_label.font()))
        self._clear_completed_btn = QPushButton("Clear completed")
        self._clear_completed_btn.clicked.connect(self._on_clear_completed_clicked)
        self._clear_completed_btn.setEnabled(False)
        self._clear_log_btn = QPushButton("Clear log")
        self._clear_log_btn.clicked.connect(self._on_clear_log_clicked)
        queue_header_row.addWidget(queue_label)
        queue_header_row.addStretch()
        queue_header_row.addWidget(self._clear_completed_btn)
        queue_header_row.addWidget(self._clear_log_btn)
        root.addLayout(queue_header_row)

        self._queue_list = QListWidget()
        self._queue_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        root.addWidget(self._queue_list, stretch=1)

        # Start/Stop + status row
        action_row = QHBoxLayout()
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self._on_start_or_stop_clicked)
        self._status_label = QLabel("Idle")
        self._status_label.setStyleSheet("color: #666;")
        action_row.addWidget(self._start_btn)
        action_row.addWidget(self._status_label, stretch=1)
        root.addLayout(action_row)

        # Progress bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 5)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%v / %m — %p%")
        root.addWidget(self._progress_bar)

        # Log area
        log_label = QLabel("Log")
        log_label.setFont(_bold(log_label.font()))
        root.addWidget(log_label)
        self._log_output = QTextEdit()
        self._log_output.setReadOnly(True)
        self._log_output.setFontFamily("monospace")
        root.addWidget(self._log_output, stretch=1)

        return page

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Refresh the Library tab whenever it becomes visible."""
        # Library tab is index 1. Refresh on every entry so new ingests
        # appear without the user needing to click Refresh.
        if self._tabs.widget(index) is self._library_view:
            self._library_view.refresh()

    @Slot()
    def _on_settings_clicked(self) -> None:
        """Open the Settings dialog. Saved changes apply on the next run."""
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._append_log("Settings saved. They'll apply to the next ingest.")

    @Slot()
    def _on_doctor_clicked(self) -> None:
        """Run environment checks and show results in a dialog."""
        results = run_checks()
        lines: list[str] = []
        for r in results:
            symbol = "✓" if r.ok else ("✗" if r.required else "○")
            required_note = "" if r.required else " (optional)"
            lines.append(f"{symbol} {r.name}{required_note}: {r.detail}")
        report = "\n".join(lines)

        box = QMessageBox(self)
        box.setWindowTitle("Doctor — environment check")
        box.setText("Environment check results:")
        box.setDetailedText(report)
        # Show details expanded by default so the user sees results
        # without clicking "Show Details".
        for btn in box.buttons():
            if box.buttonRole(btn) == QMessageBox.ButtonRole.ActionRole:
                btn.click()
                break
        box.exec()

    @Slot()
    def _on_reconcile_clicked(self) -> None:
        """Prune library index entries for folders that no longer exist."""
        try:
            kept, removed = reconcile_library_index()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Reconcile failed",
                f"Could not reconcile library: {e}",
            )
            return
        QMessageBox.information(
            self,
            "Reconcile complete",
            f"Kept {kept} entries. Removed {removed} entries for deleted folders.",
        )
        # Reflect the change in the Library tab immediately.
        self._library_view.refresh()

    @Slot()
    def _on_about_clicked(self) -> None:
        QMessageBox.about(
            self,
            "About Claude Video Ingest",
            f"<h3>Claude Video Ingest {__version__}</h3>"
            f"<p>Turn YouTube videos into Claude-ready reference folders.</p>"
            f"<p>Drag <b>START-HERE-for-Claude.md</b> into a Claude chat, "
            f"then drag each <b>batch-N</b> folder in order.</p>"
            f"<p style='color:#666;'>MIT License. Copyright © 2026 Aidan Shephard.</p>",
        )

    # ------------- UI event handlers -------------

    @Slot()
    def _on_add_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            return
        try:
            parse_youtube_url(url)
        except VideoIngestError as e:
            QMessageBox.warning(self, "Invalid URL", e.what)
            return
        if any(i.url == url for i in self._items):
            QMessageBox.information(
                self, "Already queued", "That URL is already in the queue."
            )
            return

        item = QueueItem(url=url)
        self._items.append(item)
        self._append_item_row(item)

        self._url_input.clear()
        self._url_input.setFocus()
        self._refresh_controls()

    @Slot()
    def _on_start_or_stop_clicked(self) -> None:
        if self._processing:
            # Currently running — click means Stop.
            self._processing = False
            self._status_label.setText("Stopping after current step...")
            if self._worker is not None:
                self._worker.request_cancel()
            self._refresh_controls()
            return

        # Idle — click means Start.
        if not any(i.status == QueueStatus.PENDING for i in self._items):
            self._append_log("Queue is empty or has no pending items.")
            return

        self._processing = True
        self._append_log("\n=== Starting queue ===")
        self._refresh_controls()
        self._run_next_pending()

    @Slot()
    def _on_clear_completed_clicked(self) -> None:
        """Remove all items with terminal status (done/failed/cancelled)."""
        survivors: list[QueueItem] = []
        kept_rows: list[int] = []
        for i, item in enumerate(self._items):
            if item.status in (
                QueueStatus.DONE,
                QueueStatus.FAILED,
                QueueStatus.CANCELLED,
            ):
                continue
            survivors.append(item)
            kept_rows.append(i)

        # Rebuild the list widget in-place
        self._queue_list.clear()
        self._items = survivors
        for item in self._items:
            self._append_item_row(item)
        self._refresh_controls()

    @Slot(str)
    def _on_item_remove_clicked(self, url: str) -> None:
        """
        ✕ button on a row clicked. What happens depends on that item's status:
          pending → drop it from the queue
          running → request cancellation (it'll be marked cancelled when it stops)
          done/failed/cancelled → remove from the list
        """
        item = self._find_item(url)
        if item is None:
            return

        if item.status == QueueStatus.RUNNING:
            # Don't remove now; let the worker finish its current step and
            # emit cancelled_item, then the normal completion flow runs.
            # But set a flag so we know to remove it (not just mark it) once stopped.
            item.status = QueueStatus.RUNNING  # unchanged
            self._pending_removal_after_cancel = url
            if self._worker is not None:
                self._worker.request_cancel()
            self._status_label.setText("Stopping current item after current step...")
            return

        # Pending or terminal — remove immediately.
        self._remove_item(url)
        self._refresh_controls()

    # ------------- Worker management -------------

    def _run_next_pending(self) -> None:
        """Kick off the next pending item. No-op if none or _processing is off."""
        if not self._processing:
            return
        next_item = next(
            (i for i in self._items if i.status == QueueStatus.PENDING), None
        )
        if next_item is None:
            self._processing = False
            self._status_label.setText("Queue drained")
            self._append_log("\n=== Queue drained ===")
            self._refresh_controls()
            return

        self._running_item = next_item
        next_item.status = QueueStatus.RUNNING
        self._refresh_row(next_item)

        # Load fresh settings for each job so changes made during queue
        # processing apply to subsequent items.
        saved = load_settings()
        settings = IngestSettings(
            url=next_item.url,
            use_whisper_fallback=saved.use_whisper_fallback,
            whisper_model=saved.whisper_model,
            max_frames=saved.max_frames,
        )
        thread = QThread(self)
        worker = IngestWorker(settings)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._wire_progress)
        worker.started_item.connect(self._on_worker_started)
        worker.finished_item.connect(self._on_worker_finished)
        worker.failed_item.connect(self._on_worker_failed)
        worker.cancelled_item.connect(self._on_worker_cancelled)

        # Teardown after any terminal signal
        worker.finished_item.connect(lambda *_: self._teardown_worker())
        worker.failed_item.connect(lambda *_: self._teardown_worker())
        worker.cancelled_item.connect(lambda *_: self._teardown_worker())

        self._thread = thread
        self._worker = worker
        thread.start()

    def _teardown_worker(self) -> None:
        if self._thread is None:
            return
        self._thread.quit()
        self._thread.wait(3000)
        self._thread = None
        self._worker = None
        self._running_item = None

        # If a removal was requested mid-run, apply it now.
        pending = getattr(self, "_pending_removal_after_cancel", None)
        if pending is not None:
            self._remove_item(pending)
            self._pending_removal_after_cancel = None

        self._progress_bar.setValue(0)
        self._refresh_controls()

        # Auto-advance if processing is still on and pending items exist
        if self._processing:
            self._run_next_pending()
        else:
            self._status_label.setText("Idle")

    # ------------- Signal bridges from worker -------------

    @Slot(object)
    def _wire_progress(self, progress: QtProgress) -> None:
        progress.step_changed.connect(self._on_progress_step)
        progress.substep_logged.connect(lambda s: self._append_log(f"  {s}"))
        progress.ok_logged.connect(lambda m: self._append_log(f"  ✓ {m}"))
        progress.warn_logged.connect(lambda m: self._append_log(f"  ! {m}"))
        progress.frame_progress_changed.connect(self._on_frame_progress)

    @Slot(int, int, str)
    def _on_progress_step(self, current: int, total: int, label: str) -> None:
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(current)
        self._status_label.setText(f"Step {current}/{total}: {label}")
        self._append_log(f"\n[{current}/{total}] {label}...")

    @Slot(int, int)
    def _on_frame_progress(self, n: int, total: int) -> None:
        if total <= 10 or n == total or n % max(1, total // 10) == 0:
            self._append_log(f"    frame {n}/{total}")

    @Slot(str)
    def _on_worker_started(self, url: str) -> None:
        self._begin_run_log_section(url)

    def _begin_run_log_section(self, url: str) -> None:
        """Append a visual separator for a new ingest run to distinguish it
        from previous runs in the log pane."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        separator = "─" * 60
        self._log_output.append(f"\n{separator}")
        self._log_output.append(f"[{timestamp}] Starting: {url}")
        self._log_output.append(separator)

    @Slot(str, str)
    def _on_worker_finished(self, url: str, folder: str) -> None:
        item = self._find_item(url)
        if item is None:
            return
        item.status = QueueStatus.DONE
        item.result_folder = Path(folder)
        self._refresh_row(item)
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._append_log(f"\n✓ Done. Folder: {folder}")
        # Refresh the Library tab so the new folder appears immediately.
        # The tab itself may not be visible right now, but refreshing is
        # cheap and makes the user's next click on the tab show fresh data.
        self._library_view.refresh()

    @Slot(str, str, str)
    def _on_worker_failed(self, url: str, what: str, fix: str) -> None:
        item = self._find_item(url)
        if item is None:
            return
        item.status = QueueStatus.FAILED
        item.error_what = what
        item.error_fix = fix
        self._refresh_row(item)
        self._append_log(f"\n✗ Failed: {what}")
        if fix:
            self._append_log(fix)
        # Rich error dialog with Copy/Open-log affordances.
        ErrorDialog(url, what, fix, parent=self).exec()

    @Slot(str)
    def _on_worker_cancelled(self, url: str) -> None:
        item = self._find_item(url)
        if item is None:
            return
        item.status = QueueStatus.CANCELLED
        self._refresh_row(item)
        self._append_log(f"\n⊘ Cancelled: {url}")

    # ------------- Row management helpers -------------

    def _append_item_row(self, item: QueueItem) -> None:
        widget = QueueItemWidget(item)
        widget.removal_requested.connect(self._on_item_remove_clicked)
        list_item = QListWidgetItem()
        list_item.setData(Qt.ItemDataRole.UserRole, item.url)
        list_item.setSizeHint(widget.sizeHint())
        self._queue_list.addItem(list_item)
        self._queue_list.setItemWidget(list_item, widget)

    def _refresh_row(self, item: QueueItem) -> None:
        row = self._find_row_for_url(item.url)
        if row is None:
            return
        list_item = self._queue_list.item(row)
        widget = self._queue_list.itemWidget(list_item)
        if isinstance(widget, QueueItemWidget):
            widget.refresh()

    def _remove_item(self, url: str) -> None:
        row = self._find_row_for_url(url)
        if row is None:
            return
        self._queue_list.takeItem(row)
        self._items = [i for i in self._items if i.url != url]

    def _find_item(self, url: str) -> QueueItem | None:
        return next((i for i in self._items if i.url == url), None)

    def _find_row_for_url(self, url: str) -> int | None:
        for row in range(self._queue_list.count()):
            list_item = self._queue_list.item(row)
            if list_item.data(Qt.ItemDataRole.UserRole) == url:
                return row
        return None

    def _refresh_controls(self) -> None:
        """Keep Start/Stop label + Clear Completed enablement in sync with state."""
        if self._processing:
            self._start_btn.setText("Stop")
        else:
            self._start_btn.setText("Start")

        has_pending = any(i.status == QueueStatus.PENDING for i in self._items)
        self._start_btn.setEnabled(self._processing or has_pending)

        has_completed = any(
            i.status in (QueueStatus.DONE, QueueStatus.FAILED, QueueStatus.CANCELLED)
            for i in self._items
        )
        self._clear_completed_btn.setEnabled(has_completed and not self._processing)

    def _append_log(self, text: str) -> None:
        self._log_output.append(text)
        scrollbar = self._log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot()
    def _on_clear_log_clicked(self) -> None:
        self._log_output.clear()


def _bold(font: QFont) -> QFont:
    f = QFont(font)
    f.setBold(True)
    return f


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def gui_main(argv: list[str] | None = None) -> int:
    """
    Launch the GUI. Returns the Qt exit code.

    Called from cli.main() when the binary is invoked with no CLI args
    (i.e. double-clicked). Also exposed as a console_script for editable
    installs during development.
    """
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Claude Video Ingest")
    app.setApplicationVersion(__version__)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(gui_main())
