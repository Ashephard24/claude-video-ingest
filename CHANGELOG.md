# Changelog

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
