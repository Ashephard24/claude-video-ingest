"""
PyInstaller entry point. Imports video_ingest.cli as a proper module
(not a script), so relative imports inside the package work at runtime.

This file is not part of the installable package — it only exists for
PyInstaller to consume as the frozen entry point.
"""

import sys

from video_ingest.cli import main

if __name__ == "__main__":
    sys.exit(main())
