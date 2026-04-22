# Building Claude Video Ingest from source

This doc is for producing binary releases locally. For CI-driven releases, see `.github/workflows/build.yml` (covered in the CI milestone).

## Prerequisites

- Python 3.11 or newer (3.10 works but CI targets 3.11)
- Windows 10+, macOS 12+, or Linux with glibc 2.31+
- A working C compiler toolchain (for building `ctranslate2` wheels if a prebuilt isn't available for your platform — rare, but possible)

## Quick build

From the repo root:

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

python build.py
```

That produces the binary in `dist/`:

- Windows: `dist/ClaudeVideoIngest.exe`
- macOS: `dist/ClaudeVideoIngest.app`
- Linux: `dist/ClaudeVideoIngest`

## What `build.py` does

1. Verifies you're in a virtualenv (safety check)
2. Runs `pip install -e ".[build]"` — installs the app and PyInstaller
3. Cleans any previous `dist/` and `build/` directories
4. Invokes `pyinstaller build.spec --clean --noconfirm`
5. Prints the output path and file size

## Manual build

If you want to invoke PyInstaller directly:

```
pip install -e ".[build]"
pyinstaller build.spec --clean --noconfirm
```

Same result, slightly more control (add your own PyInstaller flags if needed).

## Build output size expectations

| Platform | Approx size | Notes |
|---|---|---|
| Windows | ~550–650 MB | Includes PySide6, faster-whisper, imageio-ffmpeg, yt-dlp |
| macOS | ~500–600 MB | Same; .app bundle structure adds minor overhead |
| Linux | ~500–600 MB | Same; smallest of the three |

The first transcription using a Whisper model will download the model (~140 MB for `base`) to `~/.cache/huggingface/` on first use. The binary itself doesn't ship any Whisper models — this keeps the download size manageable and lets power users upgrade to `small`/`medium`/`large` without reshipping.

## Testing the binary

After a successful build, verify the three modes work:

```
# Windows (replace .exe with the platform-appropriate binary)
dist\ClaudeVideoIngest.exe --doctor
dist\ClaudeVideoIngest.exe --version
dist\ClaudeVideoIngest.exe                       # should open the GUI
dist\ClaudeVideoIngest.exe "https://youtube.com/watch?v=..."   # CLI ingest
```

Expected:

- `--doctor` lists all the environment checks, all passing. ffmpeg should show a path inside the unpacked PyInstaller bundle.
- `--version` prints `2.0.0`.
- No args, double-click: the GUI opens with Queue and Library tabs.
- With a YouTube URL from a terminal: runs the CLI ingest as in v1.2.2.

## Known issues / platform quirks

### Windows: SmartScreen warning on first download

The binary is unsigned, so Windows will block it with a SmartScreen warning when first downloaded:

1. Browser warns: "this file might harm your device" → click **Keep** (or "Keep anyway")
2. Run the file: Windows shows "Windows protected your PC" → click **More info** → click **Run anyway**

This is a one-time step per downloaded version. Code signing would remove this friction; it's not in scope for v2.0.

### macOS: Gatekeeper block on first launch

The `.app` is unsigned and unnotarized. First-launch attempt:

1. Double-click shows: *"Claude Video Ingest cannot be opened because Apple cannot check it for malicious software."*
2. Click **Cancel** (don't trash it).
3. Open **System Settings → Privacy & Security**
4. Scroll to the message about the blocked app
5. Click **Open Anyway**, then click **Open** on the next confirmation dialog
6. Enter your password if prompted

Subsequent launches work normally.

### Linux: no signing-style friction

AppImage or raw ELF binary. First-run needs the execute bit:

```
chmod +x ClaudeVideoIngest
./ClaudeVideoIngest
```

## Troubleshooting local builds

**"pyinstaller: command not found"** — you're not in the venv, or `pip install -e ".[build]"` didn't succeed. Activate the venv and re-run.

**"ImportError: libctranslate2..."** on Linux — your distro's glibc is too old. Build on a newer distro or use the CI-produced artifact.

**Output binary is 1.5GB+** — you likely have extra packages in your venv that PyInstaller is sweeping up. Create a fresh venv and only install the project's declared deps: `pip install -e ".[build]"`.

**Build fails with "could not find ..."** — open an issue with the exact error text from the PyInstaller output. Paraphrased descriptions make it nearly impossible to diagnose.
