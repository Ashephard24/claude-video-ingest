# Contributing to Claude Video Ingest

Thanks for considering a contribution.

## Bug reports

The fastest path to a fix:

1. **Hit the bug in the app.** When the error dialog appears, click **Copy error details**. This puts a markdown-formatted report on your clipboard with version, timestamp, URL, error message, and (for unexpected errors) a pointer to the full log file.
2. **[Open an issue](https://github.com/Ashephard24/claude-video-ingest/issues/new/choose)** using the Bug Report template.
3. **Paste the copied details** into the issue body. If the error mentions a log file, also attach the log file itself (`.last-error.log` in `~/Documents/claude-video-library/`).
4. **Describe what you were trying to do** — the URL alone is usually enough, but if there's a specific setting change involved, mention it.

What NOT to do: paraphrase the error. Real error text is the difference between a one-hour fix and a one-week guessing game.

## Feature requests

Open an issue using the Feature Request template. Before writing one, check the CHANGELOG — some things are deliberately out of scope for v2.0 (code signing, parallel processing, self-updater) and won't be reconsidered without strong new information.

## Pull requests

1. **Fork, branch from `main`.**
2. **Set up the dev environment:**
   ```
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```
3. **Make changes. Add or update tests.** The test suite is `pytest tests/` and runs in under 3 seconds. There's no excuse not to run it before pushing.
4. **Don't break existing tests.** v2.0 preserves byte-compatibility with v1.2.2 output. If your change alters the folder layout, filenames, or file contents produced for a given input, that's a breaking change and needs explicit sign-off.
5. **Don't reshape the package structure** to make PyInstaller happy. Work around tool limits.
6. **Keep the GUI code in `src/video_ingest/gui/`.** The pipeline and CLI must continue to work without PySide6 installed (CLI-only dev installs).
7. **Open a PR** with a clear description of what changed and why. Link to the issue you're addressing if there is one.

### CI must be green

Every PR runs the full test suite on Windows, macOS, and Linux. If any platform fails, the PR won't merge. If you can't figure out why CI is failing on a platform you don't have, tag it in the PR — someone with that OS can help.

### Release process (maintainers)

1. Update `__version__` in `src/video_ingest/__init__.py`.
2. Update `version` in `pyproject.toml` to match.
3. Add a new entry to `CHANGELOG.md` at the top.
4. Commit, push, and tag: `git tag v2.0.1 && git push --tags`.
5. GitHub Actions builds all three platforms in parallel and creates a Release automatically (~15–20 minutes). The CHANGELOG entry becomes the release body.
6. If the tag doesn't match `__version__`, CI fails fast — fix the mismatch and retag.

## Code style

- **Python**: type hints where they add clarity, `from __future__ import annotations` in all files, 4-space indent, no semicolons. Existing code establishes the tone; match it.
- **Qt / GUI**: signal/slot connections preferred over direct method calls where the concern is cross-thread or cross-widget. Don't reach into private attributes of other widgets.
- **Error messages**: every user-facing error goes through a `VideoIngestError` subclass with `what` (what went wrong) and `fix` (concrete next steps). No bare raises with string messages. No cryptic stack traces reaching the user — those belong in the error log.
- **Logs**: use `logging`, not `print`. `print` is for CLI user-facing output via Rich only.

## Non-goals

Claude Video Ingest is deliberately narrow. The following are off-scope unless you have a very compelling case:

- Video *editing* features (trimming, format conversion, etc.)
- Non-YouTube sources (yt-dlp technically supports many sites but we don't test or maintain for them)
- Self-update mechanism
- Cloud storage / multi-user features
- Parallel video processing

## License

By contributing, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
