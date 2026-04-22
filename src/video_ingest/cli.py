"""
Command-line interface for video-ingest.

Separated from the pipeline so the core logic can be used as a library too.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .doctor import all_required_passing, run_checks
from .errors import VideoIngestError
from .paths import error_log_path
from .pipeline import Progress, ingest
from .utils import parse_youtube_url

console = Console()


class RichProgress(Progress):
    """Rich-powered progress reporter for the CLI."""

    def __init__(self) -> None:
        self._current_step = 0
        self._current_frame_total = 0

    def step(self, current: int, total: int, label: str) -> None:
        console.print()
        console.print(f"[bold cyan][{current}/{total}][/] {label}...")
        self._current_step = current

    def substep(self, label: str) -> None:
        console.print(f"      [dim]{label}[/]")

    def ok(self, message: str) -> None:
        console.print(f"      [green]✓[/] {message}")

    def warn(self, message: str) -> None:
        console.print(f"      [yellow]![/] {message}")

    def frame_progress(self, n: int, total: int) -> None:
        # Avoid spam: only print every 10% or so
        if total <= 10 or n == total or n % max(1, total // 10) == 0:
            console.print(f"      [dim]frame {n}/{total}[/]")


def _render_error(err: VideoIngestError) -> None:
    """Render a user-facing error message nicely."""
    body = Text()
    body.append("What went wrong:\n", style="bold")
    body.append(f"  {err.what}\n\n")
    if err.fix:
        body.append("How to fix it:\n", style="bold")
        for line in err.fix:
            if line:
                body.append(f"  {line}\n")
            else:
                body.append("\n")
    console.print()
    console.print(Panel(body, title="[red]✗ video-ingest failed[/]", border_style="red"))


def write_error_log(exc: BaseException) -> Path:
    """
    Write a full traceback for an unexpected exception to the error log.
    Shared between CLI and GUI so both surfaces produce identical logs.
    Returns the path the log was written to.
    """
    log_path = error_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"video-ingest {__version__}\n")
        f.write("=" * 72 + "\n")
        f.write(f"Python: {sys.version}\n\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    return log_path


def _dump_unexpected_error(exc: BaseException) -> None:
    """Dump an unexpected exception to the error log and tell the user where it went."""
    log_path = write_error_log(exc)

    body = Text()
    body.append("Something unexpected went wrong.\n\n", style="bold")
    body.append("Full error written to:\n", style="bold")
    body.append(f"  {log_path}\n\n")
    body.append("What to do:\n", style="bold")
    body.append("  1. Try running the same command again (often transient).\n")
    body.append("  2. If it keeps failing, open that log file and paste its\n")
    body.append("     contents into a Claude chat along with the URL you used.\n")
    body.append("     Claude can help diagnose what's wrong.\n")
    console.print()
    console.print(Panel(body, title="[red]✗ Unexpected error[/]", border_style="red"))


def cmd_doctor() -> int:
    """Run the doctor diagnostic checks."""
    console.print()
    console.print("[bold]Running diagnostics...[/]")
    console.print()

    results = run_checks()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Detail")

    for r in results:
        status = "[green]✓ OK[/]" if r.ok else ("[red]✗ FAIL[/]" if r.required else "[yellow]- optional[/]")
        table.add_row(r.name, status, r.detail)

    console.print(table)
    console.print()

    if all_required_passing(results):
        console.print("[green]All required components OK.[/] You're ready to ingest videos.")
        return 0
    else:
        console.print("[red]Some required components are missing.[/] Fix the failures above, then re-run --doctor.")
        return 1


def cmd_reconcile() -> int:
    """Prune library.md entries for deleted video folders."""
    from .library import reconcile_library_index
    from .paths import library_index_path

    console.print()
    if not library_index_path().exists():
        console.print("[yellow]No library.md found.[/] Nothing to reconcile.")
        return 0

    kept, removed = reconcile_library_index()
    if removed == 0:
        console.print(f"[green]Library is up to date.[/] {kept} entries, 0 removed.")
    else:
        console.print(
            f"[green]Library reconciled.[/] Kept {kept} entries, "
            f"removed {removed} stale entr{'y' if removed == 1 else 'ies'}."
        )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Run the ingest pipeline."""
    try:
        # Validate URL early
        parse_youtube_url(args.url)
    except VideoIngestError as e:
        _render_error(e)
        return 2

    console.print()
    console.print(Panel(
        f"[bold]Ingesting:[/] {args.url}",
        border_style="cyan",
    ))

    progress = RichProgress()

    try:
        folder = ingest(
            args.url,
            use_whisper_fallback=not args.no_whisper,
            whisper_model=args.whisper_model,
            max_frames=args.max_frames,
            min_frame_interval=args.min_frame_interval,
            scene_threshold=args.scene_threshold,
            batch_size=args.batch_size,
            progress=progress,
        )
    except VideoIngestError as e:
        _render_error(e)
        return 2
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Interrupted by user.[/]")
        return 130
    except Exception as e:  # noqa: BLE001
        _dump_unexpected_error(e)
        return 3

    # Success
    console.print()
    success = Text()
    success.append("Done.\n\n", style="bold green")
    success.append("Folder: ", style="bold")
    success.append(f"{folder}\n\n")
    success.append("Next step: ", style="bold")
    success.append("drag ")
    success.append("START-HERE-for-Claude.md", style="bold cyan")
    success.append(" into a new Claude chat, send, then drag in ")
    success.append("batch-1/", style="bold cyan")
    success.append(" contents. For multi-batch videos, continue with batch-2/, batch-3/, etc.")
    console.print(Panel(success, title="[green]✓ Success[/]", border_style="green"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-ingest",
        description=(
            "Ingest a YouTube video into your Claude video library. "
            "Downloads the video, extracts transcript and frames, and writes "
            "everything into a folder you can drag into a Claude chat."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  video-ingest https://youtube.com/watch?v=dQw4w9WgXcQ
  video-ingest --max-frames 100 https://youtu.be/VIDEO_ID
  video-ingest --no-whisper https://youtube.com/watch?v=VIDEO_ID
  video-ingest --doctor
""",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="YouTube URL or video ID to ingest.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run diagnostic checks and exit.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help=(
            "Prune library.md entries for video folders that no longer exist "
            "on disk. Useful after deleting video folders manually. "
            "Runs automatically on every ingest."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"video-ingest {__version__}",
    )
    parser.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable Whisper fallback. If the video has no captions, fail instead.",
    )
    parser.add_argument(
        "--whisper-model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model to use for the fallback (default: base). Larger = more accurate but slower.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=60,
        help="Maximum number of frames to extract (default: 60).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=18,
        help=(
            "Files per upload batch when batching kicks in (default: 18; "
            "Claude.ai caps at 20 per message). Only used when total files > 20."
        ),
    )
    parser.add_argument(
        "--min-frame-interval",
        type=float,
        default=30.0,
        help="Maximum seconds between consecutive frames, in seconds (default: 30).",
    )
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=0.35,
        help="Scene detection sensitivity 0.0-1.0 (default: 0.35; higher = fewer scenes detected).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging to stderr.",
    )
    return parser


def _no_cli_args_provided(argv: list[str] | None) -> bool:
    """
    Did the user invoke the program with zero CLI arguments?

    This is how we detect the double-click case in the PyInstaller-built
    binary: a double-click launches the executable with argv = [exe_path]
    and nothing else, so sys.argv[1:] is empty. In that case we want to
    open the GUI instead of printing parser help to a (possibly non-existent)
    terminal.

    We pass through explicit argv too, for testability.
    """
    source = argv if argv is not None else sys.argv[1:]
    return len(source) == 0


def main(argv: list[str] | None = None) -> int:
    # If the user gave us no arguments at all, treat this as the GUI launch
    # path. This covers double-clicking the binary on Windows/macOS/Linux.
    # Any explicit flag or URL keeps the CLI path intact — v1.2.2 behavior
    # is byte-identical for terminal users.
    if _no_cli_args_provided(argv):
        try:
            from .gui.app import gui_main
        except ImportError as e:
            console.print(
                f"[red]GUI launch failed:[/] {e}\n"
                "Install GUI dependencies with: pip install PySide6"
            )
            return 1
        return gui_main(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    if args.doctor:
        return cmd_doctor()

    if args.reconcile:
        return cmd_reconcile()

    if not args.url:
        parser.print_help()
        return 0

    return cmd_ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
