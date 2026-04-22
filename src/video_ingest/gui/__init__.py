"""Desktop GUI for Claude Video Ingest.

Built on PySide6. The GUI is a thin layer over the existing pipeline:
all the real work happens in `video_ingest.pipeline` — the GUI just
provides a Qt-friendly Progress reporter and a window.

This package is imported only when launching the GUI. The CLI
(`video_ingest.cli`) does not import anything from here, so the
CLI still works in environments without PySide6 installed.
"""
