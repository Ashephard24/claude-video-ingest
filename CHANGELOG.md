# Changelog

## 2.1.3 — 2026-04-23

**Bug fixes for library readability (v2.1.2 smoke test findings).**

### Fixed

- **Selected library rows are readable again.** The custom two-line row widgets introduced in v2.1.2 didn't swap their text colors when selected, so the bold title and muted-gray subtitle sat on top of Qt's blue selection background with near-zero contrast. Row labels now use the system highlighted-text palette color when selected, and revert on deselection. Works on light and dark system themes.
- **Long video titles no longer truncate with an ellipsis.** Titles in library rows now wrap to a second line if they don't fit on one. Rows grow vertically as needed. Titles longer than 2 lines still elide on the second line.
- **Library pane splitter actually resizes.** v2.1.2's splitter collapsed the library pane to nothing when dragged and couldn't be pulled back out. Both panes now have a minimum width and cannot be fully collapsed. Users can drag the handle to widen the library list when they have long titles.
- **Settings dialog shows the full library path.** The Library location field was narrow enough that most of the path was hidden. The dialog is wider and the field stretches to fill available horizontal space. A tooltip also shows the full path on hover.

## 2.1.2 — 2026-04-23

**Polish release: accuracy ambiguity-flagging, library rework for scale, configurable library location.**

### Changed

- **Library view reworked for scale.** Replaced the four-column table (where long titles truncated to "Ca...") with a two-line list: title on top, `Creator • Duration • Ingested` below. Added explicit Sort-by controls (Title / Creator / Duration / Ingested, with ascending/descending toggle), filter-as-you-type, and keyboard navigation (arrows to move, Enter to open folder, Delete to move to recycle bin, Ctrl+F to focus the filter box). Designed for hundreds of videos rather than tens.

### Added

- **Claude now flags ambiguity in speaker attribution** instead of guessing. YouTube transcripts don't label who is speaking, so pronouns like "I" and "he" are often ambiguous. The updated `START-HERE-for-Claude.md` instructs Claude to acknowledge the ambiguity and distinguish inferences from confirmed facts when the transcript doesn't clearly attribute an action to a named person. This addresses confident-but-wrong answers observed in v2.1.1 testing (e.g. swapping which of two people performed an action).
- **Library location is now configurable in Settings.** Previously hardcoded to `~/Documents/claude-video-library/`. Users can now point the library at an external drive, network share, or custom folder. Existing videos are NOT moved automatically — a warning in the Settings dialog makes this explicit, with instructions to move folders manually before or after the change. Auto-migration with progress reporting may come in a future release.

### Known limitations (carried from v2.1.0)

Frame extraction is sparse (~60 frames across a full video), and YouTube captions carry no speaker tags. Claude can cross-reference frames with the transcript but can't watch the video itself — the current consumer AI product doesn't yet support video uploads. If you need unambiguous attribution, a follow-up question with a specific timestamp usually gets Claude to the right answer. A v3 exploration of automated frame descriptions (via vision model at ingest time) is parked as a future direction if video upload support doesn't arrive first.

## 2.1.1 — 2026-04-22

**Polish release: fixes three issues found in v2.1.0 field testing.**

### Fixed

- **Claude no longer claims the transcript is missing when it was uploaded.** Per-batch `transcript.md` files (~100 KB) arrive as fetchable attachments on claude.ai, not as inline content. The v2.1.0 `START-HERE-for-Claude.md` didn't tell Claude to tool-read attachments, so Claude skimmed its immediate context, didn't see the transcript text, and concluded it was missing. v2.1.1 explicitly instructs Claude to tool-read any listed `transcript.md` before making claims about transcript availability, and forbids answering questions from frames alone.
- **Library table title column is readable again.** Titles were being truncated to 2-3 characters. The Title column now stretches to fill available width; Creator / Duration / Ingested columns size to content. Rows are taller to accommodate wrapped titles. Column widths also survive refresh now.

### Added

- **Help menu now has "Report a bug" and "View on GitHub" links** that open the repo's issue form and repo root in the default browser.

## 2.1.0 — 2026-04-22

**Usability + accuracy polish based on v2.0.1 field testing.**

### Fixed

- **Windows console window no longer flashes** during frame extraction. The packaged binary now suppresses subprocess console windows, so users can use their computer for other things while a video is being ingested in the background.
- **Help > About menu action** was declared but not firing. Now opens the About dialog correctly.

### Added

- **Library view is sortable and filterable.** Click any column header (Title, Creator, Duration, Ingested) to sort. Use the new search box to filter by title or creator as you type.
- **Delete-from-library button** in the library detail pane. Moves the video's folder to the OS recycle bin / trash (recoverable) and prunes it from the library index.
- **Watch-on-YouTube link** in the library detail pane. Also embedded in `START-HERE-for-Claude.md` and `ABOUT-this-video.md` so Claude has the URL when answering questions.
- **Clear log button** in the Queue tab. Each ingest now gets a visual separator + timestamp in the log so users can distinguish current work from earlier runs.
- **Smarter Claude prompting** in `START-HERE-for-Claude.md`. Claude is now explicitly instructed to cross-reference the transcript against frames when answering, and to scan the whole transcript for score/state commentary before committing to stroke types. Addresses a birdie-vs-eagle misidentification observed in v2.0.1 testing.

### Changed

- **Batch folders in the library content tree start collapsed** by default. Expand manually to see frame files.
- **README** now includes a "Known limitations" section about Claude occasionally missing details that require cross-referencing multiple video moments.

### Dependencies

- Added `send2trash>=1.8.0` for the delete-to-recycle-bin feature. Small, pure-Python, cross-platform.

## 2.0.1 — 2026-04-22

**Hotfix: v2.0.0 binary crashed on launch.**

- Fixed `ImportError: attempted relative import with no known parent package`
  that caused the packaged binary to crash immediately on double-click.
  Root cause: build.spec passed cli.py directly to PyInstaller as a
  script, which loaded the module outside the video_ingest package, so
  the package's relative imports (`from .gui.app import gui_main`, etc.)
  failed at runtime. Replaced with a dedicated launcher.py entry point
  that imports video_ingest.cli as a proper module.
- No functional changes. v2.0.0's source code is unaffected — only the
  packaging changed.

## 2.0.0 — 2026-04-22

**Major release: desktop GUI app, distributed as a standalone binary.**

This is a meaningful UX shift. v1.x was a CLI installed via pip. v2 is a download-and-run desktop app that bundles Python, PySide6, ffmpeg, and the faster-whisper transcription engine in a single executable. End users no longer need to install Python or any system tools.

### Added

- **Desktop GUI** with two tabs: Queue (paste URL → add → start → watch progress) and Library (browse ingested videos, see folder contents, drag files directly into a Claude chat).
- **Drag-from-app-to-Claude**: drag any file or folder in the Library tab's tree view straight into a Claude browser tab. Multi-select works. Under the hood: `QDrag` with `text/uri-list` MIME data pointing at real filesystem paths, which browsers treat identically to a drag from Explorer / Finder.
- **Queue with sequential processing**: add multiple videos, click Start once, they process one at a time. Per-item ✕ button removes pending items or cancels the running one.
- **Graceful cancellation**: cancel requests take effect at the next step boundary of the 5-step pipeline (download → transcript → frames → write → index). Mid-step (e.g. mid-Whisper) cancellation is not supported in this version; "Stop" messaging reflects this.
- **Settings dialog** (File → Settings, or Ctrl+,): max frames, Whisper model selection with size hints, enable/disable Whisper fallback. Persisted to a platform-appropriate config location.
- **Library view with master-detail layout**: list of videos on the left (newest first), selected folder's contents on the right. START-HERE file and batch folders shown first because they're the drag priority.
- **Rich error dialog**: friendly "what went wrong" + actionable "what to do" + a **Copy error details** button that puts a markdown-formatted bug report on your clipboard, plus an **Open log file** button for unexpected errors.
- **In-app update checker**: async GitHub Releases API query on launch (throttled to once per 24 hours). Shows a dismissible banner if a newer version exists.
- **Tools menu**: Run Doctor, Reconcile library (prune deleted folders from the index).
- **About dialog**: version info and license.
- **Machine-readable library index**: `library.json` sidecar kept in sync with `library.md`. GUI Library view reads the JSON; the markdown stays human-facing.
- **Cross-platform CI** via GitHub Actions: binaries built on windows-latest, macos-latest, ubuntu-latest and auto-attached to GitHub Releases on every `v*.*.*` tag push.
- **BUILDING.md** with local build instructions and platform-specific bypass steps for unsigned-binary warnings (SmartScreen, Gatekeeper).

### Changed

- **Whisper backend swapped from `openai-whisper` to `faster-whisper`.** Same models (tiny / base / small / medium / large-v3), no PyTorch dependency. Bundle size dropped from an estimated 1.5–2 GB to 500–700 MB. Transcription is also faster on CPU. The `run_whisper()` function signature is preserved; callers are unaffected.
- **ffmpeg is now bundled via `imageio-ffmpeg`** in the packaged binary. System ffmpeg still takes precedence when available (developer installs). End users no longer need to install ffmpeg separately.
- **Package renamed** from `video-ingest` to `claude-video-ingest` in `pyproject.toml`. The CLI command stays as `video-ingest` for compatibility in developer installs, and the binary is `ClaudeVideoIngest` / `ClaudeVideoIngest.exe` / `ClaudeVideoIngest.app`.
- **Pipeline accepts an optional `CancelToken`** parameter for GUI-driven cancellation. CLI calls pass None and behavior is identical to v1.2.2.
- **Doctor** now checks for `faster-whisper` instead of `openai-whisper`.

### Preserved

- Every CLI flag from v1.2.2 still works identically. Same output folder structure. Same file contents. Running the binary from a terminal with `video-ingest-style` args produces byte-equivalent results to v1.2.2.
- 72 pytest cases all still pass.

### Distribution

- Windows / macOS / Linux binaries on the [Releases page](https://github.com/Ashephard24/claude-video-ingest/releases). Unsigned for v2.0 — bypass instructions in [README.md](README.md) and [BUILDING.md](BUILDING.md).
- Developer install path (pip) preserved for contributors.

### Not included in v2.0.0

- Code signing (Windows + macOS). Flagged as a future enhancement; SmartScreen / Gatekeeper bypass steps documented instead.
- Self-updater. Update checker points at the Releases page; users re-download.
- Parallel video processing. Sequential only; Whisper + ffmpeg contention makes parallelism a net-negative UX.
- Mid-step cancellation. Step-boundary cancellation only.

## 1.2.2 — 2026-04-20

**Hotfix for batched-ingestion prompt confusion.**

- Fixed Claude replying "Received batch 1 of N" when the user dragged
  only `START-HERE-for-Claude.md` into a new chat (no actual batch
  contents yet). The 1.2.0/1.2.1 prompt conflated two distinct states:
  "I've read the plan" vs. "I've received batch 1." Claude would
  correctly follow the instruction to acknowledge batch 1, but since
  no batch contents had been uploaded, this shifted the batch counter
  by one and the final summary never arrived.
- The prompt now explicitly describes three states:
  - **STATE A**: reading this file alone → reply `Ready for batch 1 of N`
  - **STATE B**: receiving a batch folder's contents → reply `Received batch N of M`
  - **STATE C**: both the prompt and batch 1 in one message → reply `Received batch 1 of N`
- Added regression test verifying the prompt contains the three-state
  structure and explicitly forbids saying "Received batch 1" when
  responding to the prompt file alone.

## 1.2.1 — 2026-04-20

**Hotfix for 1.2.0 re-ingest crash.**

- Fixed `FileNotFoundError` when re-ingesting a video. The 1.2.0
  "wipe folder on re-ingest" logic destroyed source frames that the
  pipeline had staged inside a subfolder of the target video folder.
  1.2.1 no longer blanket-wipes the folder; instead, it cleans up
  specific known stale artifacts (UPLOAD-TO-CLAUDE.md, transcript.txt,
  old batch folders with higher indices, etc.) after the new files are
  safely written.
- Stale `frames/` folder from 1.0.0/1.1.0 layout is now cleaned up
  on re-ingest (only if it contains exclusively JPEGs — never removes
  user-added content).
- Added regression tests that stage source frames inside the target
  folder (matching the real pipeline's behavior) and verify stale
  artifacts from prior versions are cleaned up.

## 1.2.0 — 2026-04-20

**Simplified per-video folder layout and live library reconciliation.**

Per-video folder cleanup:
- Root now contains only three items: `START-HERE-for-Claude.md`,
  `metadata.json`, `transcript.srt`, plus the batch folders. No other
  clutter in root.
- `ABOUT-this-video.md`, `FRAMES-index.md`, and `transcript.md` now live
  only inside `batch-1/`. Principle: the per-video folder contains only
  what Claude needs. User guidance lives in project-level docs.
- `UPLOAD-TO-CLAUDE.md` removed entirely — it was redundant with
  `START-HERE-for-Claude.md` and belonged in project docs anyway.
- `START-HERE-for-Claude.md` now lives only in the video folder root
  (no duplicate copy in `batch-1/`). User drags it into Claude first,
  then the batch contents follow.
- Re-ingesting now cleanly wipes the existing video folder before
  writing new files, so stale artifacts from previous versions don't
  survive.

Library index:
- `library.md` is now auto-reconciled on every ingest — entries for
  video folders that no longer exist on disk are pruned automatically.
- New `video-ingest --reconcile` command for manual pruning without
  ingesting anything.

Documentation consolidation:
- Platform-specific install guides (`INSTALL-WINDOWS.md`, `-MACOS.md`,
  `-LINUX.md`) folded into README sections.
- `START-HERE.md` top-level file removed — README is the entry point.
- Final doc surface: `README.md`, `CHANGELOG.md`, `SETUP-WITH-CLAUDE.md`,
  `TROUBLESHOOTING-WITH-CLAUDE.md`, plus `LICENSE` and `.gitignore`.

Internal:
- `plan_batches` simplified — always produces a batch layout (single
  batch for small videos, multiple for large). No more flat/batched
  branching in write logic.
- `_write_upload_instructions` function removed; `_write_ingestion_prompt`
  rewritten to handle both single-batch and multi-batch cases cleanly.
- Tests updated; 70 tests passing including new reconcile regression
  tests.

## 1.1.1 — 2026-04-20

**Bug fixes and distribution polish.**

Transcript quality:
- **Fixed rolling-caption duplication** in YouTube auto-captions. VTT
  parser now dedupes at the line level instead of the cue level, so
  each sentence appears exactly once at its first timestamp. Transcripts
  are roughly half the size they were in 1.1.0.
- **Transcript file is now markdown** (`transcript.md`) instead of
  plain text. Includes a heading, brief intro, and bold-formatted
  timestamps. Easier for Claude to parse.
- **Transcripts are written with UTF-8 BOM** for maximum compatibility
  with Claude.ai's file uploader (1.1.0 occasionally had uploads
  reported as empty).

Upload UX:
- **Ingestion prompt is now drag-and-drop.** Renamed
  `CLAUDE-INGESTION-PROMPT.md` → `START-HERE-for-Claude.md`. The entire
  file contents are the prompt — no copy-paste from code blocks.
  Users drag the file into Claude as the first upload and Claude reads
  it as their instructions.
- **Fixed leftover `frames/` folder** that was duplicating all frames
  into the root folder when batched mode was used. Batched mode now
  produces only the batch subfolders.

Distribution:
- Platform-specific install guides: `INSTALL-WINDOWS.md`,
  `INSTALL-MACOS.md`, `INSTALL-LINUX.md`.
- `SETUP-WITH-CLAUDE.md` — drag into Claude for guided installation.
- `TROUBLESHOOTING-WITH-CLAUDE.md` — drag into Claude for help when
  something breaks.
- `START-HERE.md` — top-level entry point that routes users to the
  right guide.
- Standalone `LICENSE` (MIT), `.gitignore`, `CHANGELOG.md`.

## 1.1.0 — 2026-04-20

**Auto-batching for Claude.ai upload limits.**

- Videos with more than ~17 frames now auto-split into `batch-1/`,
  `batch-2/` etc. subfolders so uploads fit within Claude.ai's 20-file
  per-message limit.
- New `--batch-size N` flag (default 18).
- `CLAUDE-INGESTION-PROMPT.md` generates a structured prompt telling
  Claude to wait for all batches before answering questions.
- `UPLOAD-TO-CLAUDE.md` tailors its walkthrough for flat vs. batched
  videos.

## 1.0.0 — 2026-04-20

**Initial release.**

- yt-dlp-based download (360p video + audio).
- Auto-caption transcript extraction with Whisper fallback.
- ffmpeg scene-detection frame sampling with time-interval floor.
- Per-video folder with transcript, frames, metadata, and upload
  instructions.
- Master library index at `~/Documents/claude-video-library/library.md`.
- `--doctor` diagnostic command.
- Human-readable error messages with actionable fixes.
