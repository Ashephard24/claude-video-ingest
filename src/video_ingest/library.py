"""
Library: writes a fully-formed video folder to disk and maintains the
master library index.

Per-video folder layout (1.2.0):

  YYYY-MM-DD_slugified-title_by-creator/
    START-HERE-for-Claude.md    ← prompt file to drag into a Claude chat
    metadata.json               ← machine-readable metadata
    transcript.srt              ← timestamped SRT (optional reference)
    batch-1/
      ABOUT-this-video.md       ← text files first (sort before frames)
      FRAMES-index.md
      transcript.md
      00-00-01.jpg              ← frames named by HH-MM-SS, sort after text
      00-00-05.jpg
      ...
    batch-2/
      00-10-30.jpg
      ...
    batch-N/
      ...

The per-video folder contains ONLY what Claude needs (plus a couple of
auxiliary machine-readable files). User-facing documentation about the
tool itself lives at the project root, not inside video folders.

The library index (at <library_root>/library.md) is auto-reconciled on
every ingest: entries for folders that no longer exist on disk are
pruned, and the new entry is added.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .downloader import VideoMetadata
from .frames import ExtractedFrame
from .paths import ensure_library_root, library_index_path, library_json_path
from .transcript import TranscriptSegment, segments_to_plain_text, segments_to_srt
from .utils import format_duration, seconds_to_timestamp, slugify

logger = logging.getLogger(__name__)


# Claude.ai per-message attachment limit (as of 2026).
# Source: https://support.claude.com/en/articles/8241126-uploading-files-to-claude
# We leave headroom: target 18 files per batch (max is 20).
CLAUDE_ATTACHMENT_LIMIT = 20
DEFAULT_BATCH_SIZE = 18


@dataclass
class LibraryEntry:
    """A row in the master library index."""

    ingest_date: str
    title: str
    creator: str
    duration: str
    folder_name: str
    video_url: str


@dataclass
class BatchPlan:
    """
    Describes how the video's files will be laid out on disk and uploaded.

    If batched is False, everything lives in the root folder (flat).
    If batched is True, frames are split into `batch-N/` subfolders and
    the text files live inside `batch-1/`.
    """

    batched: bool
    batch_size: int
    text_files: list[str]
    frame_batches: list[list[ExtractedFrame]]


def compute_folder_name(metadata: VideoMetadata) -> str:
    """Human-readable, filesystem-safe, sortable folder name."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_slug = slugify(metadata.title, max_length=60)
    creator_slug = slugify(metadata.creator, max_length=30)
    return f"{today}_{title_slug}_by-{creator_slug}"


def plan_batches(
    frames: list[ExtractedFrame],
    batch_size: int = DEFAULT_BATCH_SIZE,
    attachment_limit: int = CLAUDE_ATTACHMENT_LIMIT,
) -> BatchPlan:
    """
    Distribute frames across upload batches.

    In 1.2.0, the layout is always batched — text files (ABOUT-this-video,
    FRAMES-index, transcript.md) always live inside batch-1. A small video
    with few frames simply has one batch.

    Batch 1 carries the 3 text files plus up to (batch_size - 3) frames.
    Later batches carry up to batch_size frames each.

    The `batched` flag on BatchPlan indicates whether there are multiple
    batches (for UX messaging), not a different layout.
    """
    text_files = ["ABOUT-this-video.md", "FRAMES-index.md", "transcript.md"]
    first_batch_capacity = min(batch_size, attachment_limit) - len(text_files)

    if not frames:
        return BatchPlan(
            batched=False,
            batch_size=batch_size,
            text_files=text_files,
            frame_batches=[[]],
        )

    # Does it all fit in a single batch?
    if len(frames) <= first_batch_capacity:
        return BatchPlan(
            batched=False,
            batch_size=batch_size,
            text_files=text_files,
            frame_batches=[frames],
        )

    # Multi-batch: batch 1 takes first_batch_capacity frames, rest are split
    # into batch_size chunks.
    batches: list[list[ExtractedFrame]] = []
    idx = 0
    batches.append(frames[idx : idx + first_batch_capacity])
    idx += first_batch_capacity
    while idx < len(frames):
        batches.append(frames[idx : idx + batch_size])
        idx += batch_size

    return BatchPlan(
        batched=True,
        batch_size=batch_size,
        text_files=text_files,
        frame_batches=batches,
    )


def _write_about(folder: Path, metadata: VideoMetadata, transcript_source: str) -> None:
    """Write ABOUT-this-video.md directly to the given folder."""
    folder.joinpath("ABOUT-this-video.md").write_text(
        _about_text(metadata, transcript_source), encoding="utf-8-sig"
    )


def _write_frames_index(path: Path, plan: BatchPlan) -> None:
    """
    Write FRAMES-index.md — timestamps + filenames + batch location.
    """
    total_frames = sum(len(b) for b in plan.frame_batches)
    n_batches = len(plan.frame_batches)
    lines = [
        "# Frames Index",
        "",
        f"This video has {total_frames} frames extracted at visually-distinct moments.",
        "Each frame is named by its timestamp in the video (HH-MM-SS).",
        "",
    ]
    if n_batches > 1:
        lines.append(
            f"Frames are split across {n_batches} upload batches. Each frame filename "
            "is unique, so Claude can reference any frame regardless of which "
            "batch it was uploaded in."
        )
        lines.append("")

    lines.append("| Timestamp | Filename | Batch |")
    lines.append("|-----------|----------|-------|")

    for batch_idx, batch in enumerate(plan.frame_batches, start=1):
        for frame in batch:
            total = int(frame.timestamp_seconds)
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            readable = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            lines.append(f"| {readable} | `{frame.path.name}` | batch-{batch_idx} |")

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_ingestion_prompt(folder: Path, plan: BatchPlan, metadata: VideoMetadata) -> None:
    """
    Write START-HERE-for-Claude.md to the video folder root.

    This is the ONLY user-facing entry point for a video folder. The user
    drags this file into a new Claude chat. Its contents tell Claude:
      - What video this is (title, creator, duration, URL)
      - What files will follow (from batch-1/, batch-2/, etc.)
      - How to behave (wait for all batches, cite timestamps)
      - How to respond to each batch

    When the user drags this file in alone, Claude reads it and learns
    the plan. The user then drags batch-1/ contents, then batch-2/, etc.
    """
    total_frames = sum(len(b) for b in plan.frame_batches)
    n_batches = len(plan.frame_batches)

    if n_batches == 1:
        prompt = f"""# Instructions for Claude

Hello Claude. This file is part of a bundle representing a YouTube video.
The goal is for you to reason about both what was said in the video and
what was shown on screen.

## The video

- **Title:** {metadata.title}
- **Creator:** {metadata.creator}
- **Duration:** {format_duration(metadata.duration_seconds)}
- **Source URL:** {metadata.url}

## What will follow this file

In this same message (or immediately after), the user will attach:

- `ABOUT-this-video.md` — video metadata
- `FRAMES-index.md` — maps each frame image to its timestamp
- `transcript.md` — timestamped transcript of the full video
- {total_frames} frame images — JPEGs named by timestamp (`HH-MM-SS.jpg`)

## How I'd like you to handle this

1. Read the transcript and look at the frames carefully.
2. When referencing specific moments later, cite timestamps
   (e.g. "at 4:23 the creator shows..."). The FRAMES-index.md file
   maps timestamps to frame filenames.
3. Once you've ingested everything, give me a brief 2-3 sentence summary
   of what the video is about, then wait for my questions.

Thanks. Ready when you are.
"""
    else:
        batch_descriptions = [
            f"- **Batch 1**: ABOUT-this-video.md, FRAMES-index.md, transcript.md, "
            f"and {len(plan.frame_batches[0])} frame images"
        ]
        for i, batch in enumerate(plan.frame_batches[1:], start=2):
            batch_descriptions.append(f"- **Batch {i}**: {len(batch)} frame images")
        batch_list = "\n".join(batch_descriptions)

        prompt = f"""# Instructions for Claude (Batched Upload)

Hello Claude. This file is part of a bundle representing a YouTube video.
Because the video has many frames, the user is uploading files across
{n_batches} messages. The instructions below tell you how to handle this.

## The video

- **Title:** {metadata.title}
- **Creator:** {metadata.creator}
- **Duration:** {format_duration(metadata.duration_seconds)}
- **Source URL:** {metadata.url}

## The upload plan ({n_batches} batches, {total_frames} frames + 3 text files)

{batch_list}

## How you should respond — IMPORTANT, READ CAREFULLY

There are THREE distinct states. Please respond differently in each:

**STATE A — You are reading this `START-HERE-for-Claude.md` file ALONE**
(i.e. this message contains ONLY this markdown file, no batch contents yet):
  Respond with exactly this line and nothing else:
  `Ready for batch 1 of {n_batches}. Please send batch 1 when you're ready.`

  Do NOT say "Received batch 1" here — no batch has been received yet.
  This file is just the plan.

**STATE B — You have just received the contents of a batch folder**
(i.e. this message contains `ABOUT-this-video.md`, `FRAMES-index.md`,
`transcript.md`, and/or frame images named like `00-01-23.jpg`):
  For batches 1 through {n_batches - 1}, respond with exactly one line:
  `Received batch N of {n_batches}.` No other text.

  For the final batch ({n_batches}): confirm all batches received, then give a
  brief 2-3 sentence summary of what the video is about and say you're
  ready for questions.

**STATE C — The user combined the instructions and batch 1 in ONE message**
(i.e. this message contains BOTH this file AND batch-1 contents like
`transcript.md` and frame images):
  Treat it as batch 1 received. Respond with:
  `Received batch 1 of {n_batches}.` Then wait for batch 2.

## Other rules

1. **Do NOT answer any content questions until ALL {n_batches} batches have
   been received.** The user knows this and will send all batches before
   asking anything substantive. If they do ask early, remind them you're
   still waiting for batch N of {n_batches}.

2. **When referencing specific moments later**, cite timestamps (e.g.
   "at 4:23 the creator shows..."). The FRAMES-index.md file (in batch 1)
   maps every timestamp to its frame image filename.

## What batch 1 will contain when it arrives

- `ABOUT-this-video.md` — video metadata
- `FRAMES-index.md` — frame-to-timestamp map (covers ALL {n_batches} batches)
- `transcript.md` — full timestamped transcript
- {len(plan.frame_batches[0])} frame images from the first part of the video
"""

    # Write with UTF-8 BOM so Claude's uploader has zero encoding ambiguity.
    folder.joinpath("START-HERE-for-Claude.md").write_text(
        prompt, encoding="utf-8-sig"
    )


def write_video_folder(
    folder: Path,
    metadata: VideoMetadata,
    segments: list[TranscriptSegment],
    frames: list[ExtractedFrame],
    transcript_source: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    attachment_limit: int = CLAUDE_ATTACHMENT_LIMIT,
) -> BatchPlan:
    """
    Write all per-video files to disk.

    Returns the BatchPlan used so callers can report what happened.
    """
    from .transcript import segments_to_markdown

    # Ensure folder exists. Do NOT wipe it here — the caller may have put
    # source frames inside (e.g. pipeline stages them in a subfolder of
    # the same video folder). We clean up stale files at the end instead,
    # after the new files are safely written.
    folder.mkdir(parents=True, exist_ok=True)

    plan = plan_batches(frames, batch_size=batch_size, attachment_limit=attachment_limit)
    n_batches = len(plan.frame_batches)

    # ----- Root-level files (minimal, as per 1.2.0 design) -----

    # metadata.json — machine-readable record
    folder.joinpath("metadata.json").write_text(
        json.dumps(
            {
                **metadata.to_dict(),
                "ingest_date_utc": datetime.now(timezone.utc).isoformat(),
                "transcript_source": transcript_source,
                "frame_count": len(frames),
                "batch_count": n_batches,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # transcript.srt — reference SRT transcript
    folder.joinpath("transcript.srt").write_text(
        segments_to_srt(segments),
        encoding="utf-8",
    )

    # START-HERE-for-Claude.md — the ONLY file Claude reads from root
    _write_ingestion_prompt(folder, plan, metadata)

    # ----- Batch folders (always at least batch-1) -----

    # Prepare the shared text-file content
    transcript_md = segments_to_markdown(segments, metadata.title, metadata.creator)

    # batch-1 always contains the three text files. Write them with
    # leading characters that sort before HH-MM-SS frame filenames
    # (digits 0-9). We use names starting with capital letters A, F, T
    # so they naturally sort after digits in Explorer's default sort.
    # To make text files appear FIRST despite this, we rely on Explorer's
    # default "Name" sort which puts capital letters after digits — so
    # we prefix text files with a leading zero-width hack? No — simpler:
    # Explorer sorts case-insensitively and numbers sort BEFORE letters.
    # So frames (starting with digits) naturally come BEFORE text files
    # (starting with letters). To invert that and put text first, we
    # prefix the text filenames so they sort ahead of digits. The cleanest
    # way: prepend nothing and accept that sort order is by name. BUT:
    # Aidan wants text files first. Two options:
    #   (a) Prefix text filenames with "!" or "_" — hack, changes filename
    #   (b) Accept default sort where frames come first
    # Simpler: keep the names as-is. Explorer sorts digits BEFORE letters
    # by default, so frames appear first. BUT on many Windows installs,
    # the default natural sort groups letters before digits. We can't
    # control the user's Explorer view.
    # The most robust fix: use filenames that are guaranteed to sort
    # before any HH-MM-SS frame name. `0-about.md`, `0-frames.md`,
    # `0-transcript.md` — but that changes what Claude sees when it
    # reads the files. Claude reads filenames, so they need to be meaningful.
    # Compromise: keep meaningful names. Text files are ABOUT-..., FRAMES-...,
    # transcript.md. Under Explorer's default "Name (A-Z)" sort on
    # Windows 11, digits DO come before letters in name sort — so frames
    # come first. To ensure text files come first, we use filename prefixes:
    batch_1_dir = folder / "batch-1"
    batch_1_dir.mkdir(exist_ok=True)

    # Write text files directly with clean names. Order within Explorer
    # depends on Windows sort settings; filenames themselves are what
    # Claude sees and are unchanged.
    batch_1_dir.joinpath("ABOUT-this-video.md").write_text(
        _about_text(metadata, transcript_source), encoding="utf-8-sig"
    )
    _write_frames_index(batch_1_dir / "FRAMES-index.md", plan)
    batch_1_dir.joinpath("transcript.md").write_text(
        transcript_md, encoding="utf-8-sig"
    )

    # Distribute frames across all batch folders (batch-1 gets first slice)
    for batch_idx, batch in enumerate(plan.frame_batches, start=1):
        batch_dir = folder / f"batch-{batch_idx}"
        batch_dir.mkdir(exist_ok=True)
        for frame in batch:
            shutil.copy2(frame.path, batch_dir / frame.path.name)

    # Clean up stale artifacts from previous versions of the tool.
    # Only remove files we know about — never blanket-delete the folder.
    stale_files = [
        "UPLOAD-TO-CLAUDE.md",        # removed in 1.2.0
        "CLAUDE-INGESTION-PROMPT.md", # renamed in 1.1.1
        "ABOUT-this-video.md",        # moved to batch-1 in 1.2.0
        "FRAMES-index.md",            # moved to batch-1 in 1.2.0
        "transcript.md",              # moved to batch-1 in 1.2.0
        "transcript.txt",             # renamed to transcript.md in 1.1.1
    ]
    for name in stale_files:
        stale = folder / name
        if stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass  # not fatal

    # Remove stale batch folders from a prior ingest that had MORE batches
    # (e.g. re-ingested with fewer frames). We know batch-1..n_batches are
    # valid; anything higher is stale.
    for entry in folder.iterdir():
        if entry.is_dir() and entry.name.startswith("batch-"):
            try:
                idx = int(entry.name.split("-", 1)[1])
            except ValueError:
                continue
            if idx > n_batches:
                try:
                    shutil.rmtree(entry)
                except OSError:
                    pass

    # Remove a leftover top-level frames/ folder from 1.0.0/1.1.0 layout.
    # Only safe to remove if it only contains JPEGs (no user-added content).
    stale_frames = folder / "frames"
    if stale_frames.is_dir():
        contents = list(stale_frames.iterdir())
        if contents and all(p.suffix.lower() in (".jpg", ".jpeg") for p in contents):
            try:
                shutil.rmtree(stale_frames)
            except OSError:
                pass

    return plan


def _about_text(metadata: VideoMetadata, transcript_source: str) -> str:
    """Render the text body of ABOUT-this-video.md."""
    lines = [
        f"# {metadata.title}",
        "",
        f"- **Creator:** {metadata.creator}",
        f"- **Duration:** {format_duration(metadata.duration_seconds)}",
        f"- **Source URL:** {metadata.url}",
        f"- **YouTube ID:** {metadata.video_id}",
    ]
    if metadata.upload_date:
        try:
            y, m, d = metadata.upload_date[:4], metadata.upload_date[4:6], metadata.upload_date[6:]
            lines.append(f"- **Uploaded:** {y}-{m}-{d}")
        except Exception:
            pass
    lines.append(f"- **Transcript source:** {transcript_source}")
    if metadata.description:
        lines.append("")
        lines.append("## Description (from YouTube)")
        lines.append("")
        lines.append(metadata.description)
    return "\n".join(lines) + "\n"


# ----- Library index -----


def _library_index_header() -> str:
    return """# Claude Video Library

This folder contains videos ingested by the `video-ingest` tool.
Each subfolder is one video and contains a transcript, frames, and Claude-ready instructions.

**New here?** Read `README-LIBRARY.md` in this folder for the full guide.

## Ingested Videos

"""


def _library_table_header() -> str:
    return (
        "| Ingest Date | Title | Creator | Duration | Folder | YouTube |\n"
        "|-------------|-------|---------|----------|--------|---------|\n"
    )


def update_library_index(entry: LibraryEntry) -> None:
    """Append or update an entry in the master library index."""
    ensure_library_root()
    index_path = library_index_path()

    existing_rows: list[str] = []
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        rows = re.findall(r"^\|(?! *-).+\|$", content, flags=re.MULTILINE)
        for row in rows:
            if row.strip().startswith("| Ingest"):
                continue
            existing_rows.append(row)

    new_row = (
        f"| {entry.ingest_date} "
        f"| {_md_escape(entry.title)} "
        f"| {_md_escape(entry.creator)} "
        f"| {entry.duration} "
        f"| [`{entry.folder_name}/`]({entry.folder_name}/) "
        f"| [link]({entry.video_url}) |"
    )

    folder_marker = f"[`{entry.folder_name}/`]"
    replaced = False
    updated_rows: list[str] = []
    for row in existing_rows:
        if folder_marker in row:
            updated_rows.append(new_row)
            replaced = True
        else:
            updated_rows.append(row)
    if not replaced:
        updated_rows.append(new_row)

    def row_date(row: str) -> str:
        parts = [p.strip() for p in row.strip("|").split("|")]
        return parts[0] if parts else ""

    updated_rows.sort(key=row_date, reverse=True)

    content = (
        _library_index_header()
        + _library_table_header()
        + "\n".join(updated_rows)
        + "\n"
    )
    index_path.write_text(content, encoding="utf-8")
    # Mirror to JSON sidecar so the GUI Library view has a machine-
    # readable source. Uses the same surviving-rows list we just wrote,
    # so the two files are guaranteed consistent.
    _sync_library_json_from_markdown(updated_rows)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def reconcile_library_index() -> tuple[int, int]:
    """
    Prune rows in library.md for video folders that no longer exist.
    Also keeps library.json in sync by regenerating it from the surviving rows.

    Returns (kept_count, removed_count).

    This is called automatically at the start of every ingest, and can
    also be triggered manually via the --reconcile CLI flag.
    """
    root = ensure_library_root()
    index_path = library_index_path()
    if not index_path.exists():
        # No markdown index yet, but still make sure the JSON sidecar
        # doesn't have stale entries for deleted folders.
        _rebuild_library_json_from_disk(root)
        return (0, 0)

    content = index_path.read_text(encoding="utf-8")
    rows = re.findall(r"^\|(?! *-).+\|$", content, flags=re.MULTILINE)

    kept: list[str] = []
    removed = 0
    for row in rows:
        stripped = row.strip()
        if stripped.startswith("| Ingest"):
            continue  # header row
        # Extract folder name from the `[`folder-name/`](folder-name/)` cell
        match = re.search(r"\[`([^`]+)/`\]", row)
        if not match:
            # Malformed row — keep it to avoid accidental data loss
            kept.append(row)
            continue
        folder_name = match.group(1)
        if (root / folder_name).is_dir():
            kept.append(row)
        else:
            removed += 1

    if removed == 0:
        # Still sync the JSON sidecar in case it drifted.
        _sync_library_json_from_markdown(kept)
        return (len(kept), 0)

    # Rewrite the index with only surviving rows
    def row_date(r: str) -> str:
        parts = [p.strip() for p in r.strip("|").split("|")]
        return parts[0] if parts else ""

    kept.sort(key=row_date, reverse=True)
    new_content = (
        _library_index_header()
        + _library_table_header()
        + ("\n".join(kept) + "\n" if kept else "")
    )
    index_path.write_text(new_content, encoding="utf-8")
    _sync_library_json_from_markdown(kept)
    logger.info("Library index reconciled: %d kept, %d removed", len(kept), removed)
    return (len(kept), removed)


# ---------------------------------------------------------------------------
# JSON sidecar: machine-readable mirror of library.md for the GUI Library view.
# ---------------------------------------------------------------------------

def read_library_index() -> list[LibraryEntry]:
    """
    Return all LibraryEntry rows from the JSON sidecar.

    Prefers library.json (fast, machine-readable); falls back to rebuilding
    from per-folder metadata.json files if the sidecar is missing or
    corrupt. Returns an empty list if the library is empty.

    Called from the GUI's Library view on tab switch and after each ingest.
    """
    root = ensure_library_root()
    json_path = library_json_path()

    if json_path.exists():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            entries = [LibraryEntry(**row) for row in raw.get("entries", [])]
            # Sort newest-first to match the markdown table order.
            entries.sort(key=lambda e: e.ingest_date, reverse=True)
            return entries
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning("library.json unreadable (%s); rebuilding from disk", e)

    # Fallback: walk the folder tree. Slower but resilient.
    entries = _rebuild_library_json_from_disk(root)
    return entries


def _write_library_json(entries: list[LibraryEntry]) -> None:
    """Serialize the full entries list to library.json. Overwrites atomically."""
    path = library_json_path()
    payload = {
        "version": 1,
        "entries": [
            {
                "ingest_date": e.ingest_date,
                "title": e.title,
                "creator": e.creator,
                "duration": e.duration,
                "folder_name": e.folder_name,
                "video_url": e.video_url,
            }
            for e in entries
        ],
    }
    # Atomic-ish write: write to temp file in same dir, rename.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _sync_library_json_from_markdown(surviving_rows: list[str]) -> list[LibraryEntry]:
    """
    Given the set of markdown rows we're keeping, parse them back into
    LibraryEntry instances and write the JSON sidecar. Used by
    update_library_index and reconcile_library_index to keep both files
    in sync without duplicating the state management.
    """
    entries: list[LibraryEntry] = []
    for row in surviving_rows:
        parsed = _parse_markdown_row(row)
        if parsed is not None:
            entries.append(parsed)
    _write_library_json(entries)
    return entries


def _parse_markdown_row(row: str) -> LibraryEntry | None:
    """
    Parse one markdown table row back into a LibraryEntry.
    Returns None if the row is malformed.

    Row format (from update_library_index):
      | 2026-04-21 | Title | Creator | 12:34 | [`folder-name/`](folder-name/) | [link](https://...) |

    Titles and creators may contain backslash-escaped pipes (\\|) from
    _md_escape(); we split on unescaped pipes only, then unescape.
    """
    # Split on pipes NOT preceded by a backslash. Regex negative lookbehind.
    raw_parts = re.split(r"(?<!\\)\|", row.strip())
    # First and last elements are empty strings from the leading/trailing |
    parts = [p.strip() for p in raw_parts if p.strip() != ""]
    if len(parts) < 6:
        return None
    try:
        ingest_date = parts[0]
        title = parts[1].replace("\\|", "|")
        creator = parts[2].replace("\\|", "|")
        duration = parts[3]
        folder_match = re.search(r"\[`([^`]+)/`\]", parts[4])
        if not folder_match:
            return None
        folder_name = folder_match.group(1)
        url_match = re.search(r"\]\(([^)]+)\)", parts[5])
        video_url = url_match.group(1) if url_match else ""
        return LibraryEntry(
            ingest_date=ingest_date,
            title=title,
            creator=creator,
            duration=duration,
            folder_name=folder_name,
            video_url=video_url,
        )
    except (IndexError, AttributeError):
        return None


def _rebuild_library_json_from_disk(root: Path) -> list[LibraryEntry]:
    """
    Last-resort rebuild: walk every subdirectory, read its metadata.json,
    and reconstruct the entries list. Used when library.json is missing
    or corrupt. Writes the rebuilt sidecar on the way out.
    """
    entries: list[LibraryEntry] = []
    if not root.exists():
        return entries
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # Folder name carries the ingest date prefix (YYYY-MM-DD_...)
            ingest_date = folder.name.split("_", 1)[0]
            entries.append(
                LibraryEntry(
                    ingest_date=ingest_date,
                    title=meta.get("title", folder.name),
                    creator=meta.get("creator", "Unknown"),
                    duration=format_duration(int(meta.get("duration_seconds", 0))),
                    folder_name=folder.name,
                    video_url=meta.get("url", ""),
                )
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Skipping %s during rebuild: %s", folder.name, e)
            continue
    entries.sort(key=lambda e: e.ingest_date, reverse=True)
    _write_library_json(entries)
    return entries


def write_library_readme() -> None:
    """Write README-LIBRARY.md to the library root (idempotent)."""
    root = ensure_library_root()
    path = root / "README-LIBRARY.md"
    path.write_text(LIBRARY_README, encoding="utf-8")


LIBRARY_README = """# Claude Video Library

This folder is managed by the `video-ingest` tool. It holds YouTube videos
that have been processed into a format Claude can understand: transcripts,
frames, and metadata.

## Quick reference

- **`library.md`** — master index of every video in this library.
  Open this first when you want to find a video. Auto-updated on every
  ingest and kept in sync with disk (deleted folders are pruned).
- **Each subfolder** is one video. Folder names are:
  `YYYY-MM-DD_title-slug_by-creator` — sorted by ingest date.

## To use a video with Claude

1. Open `library.md` to find the video you want.
2. Open that video's folder.
3. Drag `START-HERE-for-Claude.md` into a new Claude chat. Send.
   Claude will read it and know what to expect.
4. Drag the contents of `batch-1/` into the next message. Send.
   For multi-batch videos, continue with `batch-2/`, `batch-3/`, etc.
   until Claude confirms it has everything. Then ask questions.

## To ingest a new video

From a terminal:

    video-ingest https://youtube.com/watch?v=VIDEO_ID

Options:

    --max-frames N       Maximum frames to extract (default 60)
    --batch-size N       Files per upload batch (default 18, max 20 per Claude limits)
    --no-whisper         Disable Whisper fallback for captionless videos

## To clean up after manual deletions

If you delete a video folder by hand, `library.md` is auto-reconciled
next time you ingest. To force an immediate cleanup:

    video-ingest --reconcile

## To check if the tool is working

    video-ingest --doctor

## To get help

    video-ingest --help
"""
