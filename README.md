# Claude Video Ingest

> Turn YouTube videos into Claude-ready reference folders. Desktop app + CLI.

Claude Video Ingest downloads a YouTube video, pulls the transcript, extracts key frames, and packages everything into folders you drag straight into a Claude chat. Claude can then answer questions about the video — what was said *and* what was shown on screen — citing timestamps and referencing frames.

Claude cannot watch YouTube videos directly. This tool closes that gap.

---

## Quick install

Download the latest binary for your OS from the [Releases page](https://github.com/Ashephard24/claude-video-ingest/releases/latest):

| Platform | File |
|---|---|
| Windows | `ClaudeVideoIngest-windows.exe` |
| macOS | `ClaudeVideoIngest-macos.zip` (unzip → drag `.app` to Applications) |
| Linux | `ClaudeVideoIngest-linux` (`chmod +x` then run) |

Double-click to open the app. That's it. No Python, no pip, no ffmpeg install.

### First-launch warnings (important)

The binaries are **unsigned**, so Windows and macOS will warn you the first time you run one. These warnings are expected and one-time — they don't mean anything is wrong.

**Windows SmartScreen bypass:**

1. When the browser downloads the file it may warn "this file might harm your device." Click **Keep** or **Keep anyway**.
2. Double-click the `.exe`. Windows shows "Windows protected your PC" with only a "Don't run" button visible.
3. Click **More info** (the small link near the top).
4. Click **Run anyway**.

**macOS Gatekeeper bypass:**

1. Unzip the download. Drag `ClaudeVideoIngest.app` to your Applications folder (or anywhere).
2. Double-click shows *"Claude Video Ingest cannot be opened because Apple cannot check it for malicious software."*
3. Click **Cancel** (not Move to Trash).
4. Open **System Settings → Privacy & Security**.
5. Scroll down until you see the message about the blocked app.
6. Click **Open Anyway**. Click **Open** on the next confirmation. Enter your password if prompted.
7. The app now launches normally from this point forward.

**Linux:** no equivalent warning. Make the binary executable (`chmod +x ClaudeVideoIngest-linux`) and run it.

---

## How to use it

1. **Open the app.** Paste a YouTube URL into the Queue tab. Click **Add to Queue**. Click **Start**.
2. **Watch progress** in the log pane while it downloads, transcribes, and extracts frames.
3. **Go to the Library tab** when it's done. Click your video.
4. **Drag files into Claude** from the Library tab:
   - First, drag `START-HERE-for-Claude.md` into a new Claude chat.
   - Then drag `batch-1/`, `batch-2/`, etc. in order. Each batch folder contains a chunk of frames plus the transcript/context for that chunk.
5. **Ask Claude questions.** It can cite timestamps, reference specific frames, and answer about both spoken content and visual content.

You can queue multiple videos — they process one at a time. Cancel a running job with the ✕ button (takes effect after the current step). Settings (max frames, Whisper model) live under **File → Settings**.

---

## What the tool produces per video

```
2026-04-20_video-title_by-creator/
├── START-HERE-for-Claude.md   ← drag this into Claude first
├── metadata.json              ← machine-readable metadata
├── transcript.srt             ← timestamped SRT
├── batch-1/
│   ├── ABOUT-this-video.md    ← context for this chunk
│   ├── FRAMES-index.md
│   ├── transcript.md          ← timestamped markdown transcript
│   └── <frames>               ← up to 15 JPEGs named HH-MM-SS.jpg
├── batch-2/                   ← 18 attachments per batch
└── batch-N/
```

The batch layout exists because Claude.ai's chat interface accepts a maximum of 20 attachments per message. The tool splits frames across batches so each drag-and-drop stays within that limit.

A master index at `~/Documents/claude-video-library/library.md` (and `library.json` for machine access) lists every video you've ingested. Entries for deleted folders auto-prune on next ingest, or via **Tools → Reconcile library**.

---

## Settings

Open **File → Settings** in the app.

| Setting | Default | Effect |
|---|---|---|
| Max frames | 60 | Frames extracted per video. More = more visual context, more to upload. |
| Whisper model | base (~140 MB) | Used only when YouTube captions are missing. Larger = more accurate, slower, bigger first-use download. |
| Whisper fallback | On | If off, videos without YouTube captions fail instead of transcribing locally. |

Settings are saved to a platform-appropriate config location (`%APPDATA%\Claude Video Ingest\settings.json` on Windows; similar on macOS/Linux).

Changes apply to the next queued job. They don't reconfigure a job that's already running.

---

## Updates

The app checks GitHub Releases on launch (at most once per 24 hours). If a newer version is available, a banner appears with a Download button that links to the release. Dismiss the banner if you don't care to update.

To update: download the new binary and replace the old one. No migration step; your library stays where it is.

---

## Environment check

**Tools → Run Doctor** shows which dependencies are available. In the packaged binary everything should be bundled and pass, but if you hit a weird error the doctor output is the first thing to check.

From the CLI (see below), run `ClaudeVideoIngest --doctor`.

---

## CLI mode (power users / developers)

The binary is **dual-mode**: double-click opens the GUI, but invoking it from a terminal with arguments runs the original CLI. This preserves the full v1.x CLI workflow for terminal users.

```
ClaudeVideoIngest "https://youtube.com/watch?v=..."   # ingest a video
ClaudeVideoIngest --doctor                            # environment check
ClaudeVideoIngest --reconcile                         # prune deleted folders from library index
ClaudeVideoIngest --max-frames 100 "https://..."      # override settings per invocation
ClaudeVideoIngest --whisper-model small "https://..." # use a bigger whisper model
ClaudeVideoIngest --no-whisper "https://..."          # disable whisper fallback
ClaudeVideoIngest --version                           # print version
ClaudeVideoIngest --help                              # full CLI reference
```

All v1.x CLI flags are preserved. Output is byte-identical to v1.2.2 given the same inputs.

---

## Developer install (from source)

```
git clone https://github.com/Ashephard24/claude-video-ingest.git
cd claude-video-ingest
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
.venv\Scripts\activate         # Windows
pip install -e ".[dev]"
pytest                         # run the 72-test suite
python -m video_ingest.cli     # launch the GUI from source
python -m video_ingest.cli "https://youtube.com/watch?v=..."  # CLI from source
```

Python 3.10+ required. ffmpeg must be installed and on PATH for the developer install (bundled in the binary).

## Building binaries from source

See [BUILDING.md](BUILDING.md). Short version: `python build.py` after `pip install -e ".[build]"`.

---

## Design notes

- **Transcript source preference:** YouTube manual captions > YouTube auto-captions > Whisper (faster-whisper) fallback. Whisper only runs when the first two are unavailable.
- **Frame selection:** ffmpeg scene detection with a configurable threshold (default 0.35) and a 30-second minimum interval floor. Avoids hundreds of near-identical frames from slow-panning shots.
- **Privacy:** everything runs locally. Videos and transcripts never leave your machine (except when *you* drag them into a Claude chat, which is the whole point).
- **Storage:** everything lives under `~/Documents/claude-video-library/`. To move the library, set the `VIDEO_INGEST_LIBRARY` environment variable.

---

## License

MIT. Copyright © 2026 Aidan Shephard. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
