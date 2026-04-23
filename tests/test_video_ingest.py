"""
Tests for video-ingest.

These tests cover everything that doesn't require network access:
URL parsing, slug generation, VTT parsing, library file writing,
error handling, and the doctor diagnostic.

The real end-to-end test (downloading an actual video) has to run
on a machine with network access to YouTube.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from video_ingest.downloader import VideoMetadata
from video_ingest.errors import InvalidURLError, VideoIngestError
from video_ingest.frames import ExtractedFrame, _select_frame_timestamps
from video_ingest.library import (
    BatchPlan,
    LibraryEntry,
    compute_folder_name,
    plan_batches,
    update_library_index,
    write_library_readme,
    write_video_folder,
)
from video_ingest.transcript import (
    TranscriptSegment,
    parse_vtt,
    segments_to_plain_text,
    segments_to_srt,
)
from video_ingest.utils import (
    format_duration,
    parse_youtube_url,
    seconds_to_timestamp,
    slugify,
)


# ----- URL parsing -----


class TestParseYoutubeURL:
    def test_watch_url(self):
        assert parse_youtube_url("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_www(self):
        assert parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_with_extra_params(self):
        url = "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42&feature=share"
        assert parse_youtube_url(url) == "dQw4w9WgXcQ"

    def test_youtu_be_short(self):
        assert parse_youtube_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_shorts(self):
        assert parse_youtube_url("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed(self):
        assert parse_youtube_url("https://youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_bare_video_id(self):
        assert parse_youtube_url("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_mobile_url(self):
        assert parse_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_empty_url_raises(self):
        with pytest.raises(InvalidURLError):
            parse_youtube_url("")

    def test_non_youtube_url_raises(self):
        with pytest.raises(InvalidURLError):
            parse_youtube_url("https://vimeo.com/12345")

    def test_garbage_raises(self):
        with pytest.raises(InvalidURLError):
            parse_youtube_url("not a url at all")


# ----- Slugify -----


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("Claude + You: A Journey!") == "claude-you-a-journey"

    def test_empty(self):
        assert slugify("") == "untitled"

    def test_only_punctuation(self):
        assert slugify("!!!") == "untitled"

    def test_long_truncated(self):
        long = "a " * 100
        result = slugify(long, max_length=30)
        assert len(result) <= 30

    def test_preserves_words_on_truncate(self):
        result = slugify("this-is-a-test-string-that-is-long", max_length=20)
        # Should truncate at a hyphen boundary, not mid-word
        assert not result.endswith("-")


# ----- Duration formatting -----


class TestFormatDuration:
    def test_under_hour(self):
        assert format_duration(125) == "2:05"

    def test_over_hour(self):
        assert format_duration(3725) == "1:02:05"

    def test_zero(self):
        assert format_duration(0) == "0:00"

    def test_none(self):
        assert format_duration(None) == "unknown"


class TestSecondsToTimestamp:
    def test_basic(self):
        assert seconds_to_timestamp(65) == "00-01-05"

    def test_over_hour(self):
        assert seconds_to_timestamp(3725) == "01-02-05"

    def test_zero(self):
        assert seconds_to_timestamp(0) == "00-00-00"


# ----- VTT parsing -----


SAMPLE_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:04.000
Welcome to this tutorial

00:00:04.000 --> 00:00:08.000
Today we're going to build something cool

00:00:08.000 --> 00:00:12.000
Let me show you how it works
"""

VTT_WITH_ROLLING_CAPTIONS = """WEBVTT

00:00:00.000 --> 00:00:02.000 align:start position:0%
first line

00:00:02.000 --> 00:00:04.000 align:start position:0%
first line
second line

00:00:04.000 --> 00:00:06.000 align:start position:0%
second line
third line
"""


class TestVTTParsing:
    def test_basic_parse(self, tmp_path: Path):
        vtt = tmp_path / "test.vtt"
        vtt.write_text(SAMPLE_VTT, encoding="utf-8")
        segments = parse_vtt(vtt)
        assert len(segments) == 3
        assert segments[0].text == "Welcome to this tutorial"
        assert segments[0].start_seconds == 0.0
        assert segments[0].end_seconds == 4.0

    def test_rolling_captions_deduplicated(self, tmp_path: Path):
        """YouTube auto-captions repeat lines across cues — must dedupe."""
        vtt = tmp_path / "rolling.vtt"
        vtt.write_text(VTT_WITH_ROLLING_CAPTIONS, encoding="utf-8")
        segments = parse_vtt(vtt)
        # The VTT parser will dedupe combined text blocks. It will see:
        #   block 1: "first line"                → kept
        #   block 2: "first line second line"    → kept (different from block 1)
        #   block 3: "second line third line"    → kept
        # So 3 unique text blocks, not 5.
        assert len(segments) == 3

    def test_strips_inline_tags(self, tmp_path: Path):
        vtt_content = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c>Hello</c> <00:00:01.500>there</c>
"""
        vtt = tmp_path / "tags.vtt"
        vtt.write_text(vtt_content, encoding="utf-8")
        segments = parse_vtt(vtt)
        assert len(segments) == 1
        assert "<" not in segments[0].text
        assert ">" not in segments[0].text

    def test_rolling_youtube_captions_dedupe_correctly(self, tmp_path: Path):
        """
        Regression test for the 1.1.0 bug: YouTube rolling captions produce
        cues where each cue repeats the previous cue's last line. 1.1.0 only
        deduped at the cue level, so every line ended up in the output twice
        with different timestamps. 1.1.1 dedupes at the line level.
        """
        vtt_content = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hey guys this is Chase

00:00:02.000 --> 00:00:04.000
Hey guys this is Chase
and welcome to the video

00:00:04.000 --> 00:00:06.000
and welcome to the video
today we are going to

00:00:06.000 --> 00:00:08.000
today we are going to
look at some Claude skills
"""
        vtt = tmp_path / "rolling_yt.vtt"
        vtt.write_text(vtt_content, encoding="utf-8")
        segments = parse_vtt(vtt)

        # Should produce exactly 4 unique lines, not 8 or 6
        assert len(segments) == 4
        texts = [s.text for s in segments]
        assert texts == [
            "Hey guys this is Chase",
            "and welcome to the video",
            "today we are going to",
            "look at some Claude skills",
        ]
        # Each line should appear at the timestamp where it FIRST appeared
        assert segments[0].start_seconds == 0.0
        assert segments[1].start_seconds == 2.0
        assert segments[2].start_seconds == 4.0
        assert segments[3].start_seconds == 6.0


# ----- Transcript output -----


class TestTranscriptOutput:
    def _segments(self):
        return [
            TranscriptSegment(0.0, 3.0, "First line"),
            TranscriptSegment(3.0, 6.5, "Second line"),
            TranscriptSegment(6.5, 10.0, "Third line"),
        ]

    def test_srt_output(self):
        srt = segments_to_srt(self._segments())
        assert "1\n00:00:00,000 --> 00:00:03,000\nFirst line" in srt
        assert "2\n00:00:03,000 --> 00:00:06,500\nSecond line" in srt

    def test_plain_text_with_timestamps(self):
        text = segments_to_plain_text(self._segments(), include_timestamps=True)
        assert text.startswith("[0:00] First line")
        assert "[0:03] Second line" in text

    def test_plain_text_without_timestamps(self):
        text = segments_to_plain_text(self._segments(), include_timestamps=False)
        assert text == "First line\nSecond line\nThird line"

    def test_markdown_transcript_has_title_and_creator(self):
        from video_ingest.transcript import segments_to_markdown
        md = segments_to_markdown(self._segments(), "Cool Video", "Cool Creator")
        assert "# Transcript: Cool Video" in md
        assert "Cool Creator" in md
        # Each segment should appear with bold timestamp
        assert "**[0:00]** First line" in md
        assert "**[0:03]** Second line" in md

    def test_markdown_transcript_is_valid_markdown(self):
        from video_ingest.transcript import segments_to_markdown
        md = segments_to_markdown(self._segments(), "Title", "Creator")
        # Claude ingests markdown more reliably than flat text — key structure
        assert md.startswith("# ")  # has a heading
        assert "---" in md  # has a separator
        # No raw JSON/code-like structure that could trip uploaders


# ----- Frame selection algorithm -----


class TestFrameSelection:
    def test_respects_max_frames(self):
        scenes = [float(i) for i in range(1, 100)]
        chosen = _select_frame_timestamps(
            scene_timestamps=scenes,
            video_duration=100.0,
            max_frames=20,
            min_interval=60.0,
        )
        assert len(chosen) <= 20

    def test_enforces_interval_floor(self):
        # No scene detection hits — should still get frames via interval floor
        chosen = _select_frame_timestamps(
            scene_timestamps=[],
            video_duration=300.0,
            max_frames=60,
            min_interval=30.0,
        )
        # Should have roughly 300/30 = 10 frames
        assert len(chosen) >= 8
        # Check that no two consecutive frames are more than min_interval apart
        for a, b in zip(chosen, chosen[1:]):
            assert b - a <= 30.0 + 1.0  # small tolerance for rounding

    def test_always_includes_frame_near_start(self):
        chosen = _select_frame_timestamps(
            scene_timestamps=[],
            video_duration=120.0,
            max_frames=60,
            min_interval=30.0,
        )
        # Should include something near 1.0s
        assert chosen[0] < 5.0

    def test_clamps_to_duration(self):
        chosen = _select_frame_timestamps(
            scene_timestamps=[10.0, 20.0, 30.0],
            video_duration=25.0,  # shorter than some scenes
            max_frames=60,
            min_interval=30.0,
        )
        assert all(0 < ts < 25.0 for ts in chosen)


# ----- Folder naming -----


class TestFolderNaming:
    def test_produces_readable_name(self):
        meta = VideoMetadata(
            video_id="abc123",
            title="Frontend Design with AI",
            creator="Theo (t3)",
            duration_seconds=1453,
            upload_date="20260418",
            description="",
            url="https://youtube.com/watch?v=abc123",
            thumbnail_url=None,
            has_captions=True,
            caption_languages=["en"],
        )
        name = compute_folder_name(meta)
        assert "frontend-design-with-ai" in name
        assert "by-theo-t3" in name
        # Starts with a date
        assert name[:10].count("-") == 2

    def test_handles_unicode_title(self):
        meta = VideoMetadata(
            video_id="abc",
            title="日本語のタイトル",
            creator="Someone",
            duration_seconds=100,
            upload_date=None,
            description="",
            url="https://youtube.com/watch?v=abc",
            thumbnail_url=None,
            has_captions=False,
            caption_languages=[],
        )
        name = compute_folder_name(meta)
        # Should not crash, should produce a valid folder name
        assert name
        assert "/" not in name and "\\" not in name


# ----- Library file writing -----


class TestLibraryWrite:
    def _make_metadata(self) -> VideoMetadata:
        return VideoMetadata(
            video_id="test123",
            title="Test Video",
            creator="Test Creator",
            duration_seconds=120,
            upload_date="20260420",
            description="A test video.",
            url="https://youtube.com/watch?v=test123",
            thumbnail_url=None,
            has_captions=True,
            caption_languages=["en"],
        )

    def _make_segments(self) -> list[TranscriptSegment]:
        return [
            TranscriptSegment(0.0, 5.0, "Intro"),
            TranscriptSegment(5.0, 60.0, "Main content"),
            TranscriptSegment(60.0, 120.0, "Outro"),
        ]

    def _make_frames(self, folder: Path) -> list[ExtractedFrame]:
        """
        Create source frame files for a test.

        The source frames MUST live outside the output folder because
        write_video_folder wipes the output folder on re-ingest. We use
        a sibling 'src_frames' directory.
        """
        src_dir = folder.parent / f"{folder.name}_src_frames"
        src_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for ts in [1.0, 30.0, 90.0]:
            p = src_dir / f"{seconds_to_timestamp(ts)}.jpg"
            p.write_bytes(b"fake jpeg data")
            frames.append(ExtractedFrame(ts, p))
        return frames

    def test_write_video_folder_creates_all_files(self, tmp_path: Path):
        """
        1.2.0 layout: per-video folder root contains ONLY
        START-HERE-for-Claude.md + metadata.json + transcript.srt.
        All other files live inside batch-1/.
        """
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="YouTube auto-captions",
        )
        # Root: three files + batch folders only
        assert (folder / "START-HERE-for-Claude.md").exists()
        assert (folder / "metadata.json").exists()
        assert (folder / "transcript.srt").exists()
        assert (folder / "batch-1").is_dir()

        # Text files live inside batch-1, not root
        assert not (folder / "transcript.md").exists()
        assert not (folder / "ABOUT-this-video.md").exists()
        assert not (folder / "FRAMES-index.md").exists()
        assert not (folder / "UPLOAD-TO-CLAUDE.md").exists()
        # No leftover flat frames/ folder
        assert not (folder / "frames").exists()

        # Batch-1 contains the three text files
        assert (folder / "batch-1" / "ABOUT-this-video.md").exists()
        assert (folder / "batch-1" / "FRAMES-index.md").exists()
        assert (folder / "batch-1" / "transcript.md").exists()

    def test_metadata_json_is_valid(self, tmp_path: Path):
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="Whisper (base)",
        )
        data = json.loads((folder / "metadata.json").read_text())
        assert data["video_id"] == "test123"
        assert data["title"] == "Test Video"
        assert data["transcript_source"] == "Whisper (base)"

    def test_frames_index_lists_all_frames(self, tmp_path: Path):
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="test",
        )
        # FRAMES-index lives in batch-1 in the new layout
        index = (folder / "batch-1" / "FRAMES-index.md").read_text()
        assert "00-00-01.jpg" in index
        assert "00-00-30.jpg" in index
        assert "00-01-30.jpg" in index

    def test_per_video_folder_has_no_user_facing_docs(self, tmp_path: Path):
        """
        1.2.0 principle: the per-video folder contains ONLY what Claude
        needs — no user-facing instructions like UPLOAD-TO-CLAUDE.md.
        User guidance lives at the project root, not per-video.
        """
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="test",
        )
        assert not (folder / "UPLOAD-TO-CLAUDE.md").exists()
        assert not (folder / "CLAUDE-INGESTION-PROMPT.md").exists()
        # Nothing named README* either
        for item in folder.iterdir():
            assert not item.name.lower().startswith("readme")

    def test_ingestion_prompt_file_is_written(self, tmp_path: Path):
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="test",
        )
        assert (folder / "START-HERE-for-Claude.md").exists()
        prompt = (folder / "START-HERE-for-Claude.md").read_text()
        # Must include the video title somewhere for Claude's context
        assert "Test Video" in prompt
        # Prompt should speak directly to Claude, not be wrapped in instructions
        assert "# Instructions for Claude" in prompt

    def test_transcript_md_has_utf8_bom(self, tmp_path: Path):
        """
        Regression test for 1.1.0 upload failure. Claude.ai's uploader
        sometimes reported text files as empty when they lacked a BOM.
        In 1.2.0 transcript.md lives inside batch-1/ but is still written
        with UTF-8 BOM.
        """
        folder = tmp_path / "my-video"
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="test",
        )
        # In 1.2.0 transcript.md is inside batch-1, not root
        raw = (folder / "batch-1" / "transcript.md").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), \
            "transcript.md should be written with UTF-8 BOM"
        text = raw.decode("utf-8-sig")
        assert text.startswith("# Transcript:")

    def test_source_frames_inside_target_folder_are_not_lost(self, tmp_path: Path):
        """
        Regression test for 1.2.0 wipe bug. The real pipeline stages source
        frames inside a subfolder of the target video folder (e.g.
        <folder>/_extracted_frames/). A naive "wipe folder on re-ingest"
        implementation destroys those frames before they can be copied to
        the batch folders, causing a FileNotFoundError downstream.

        write_video_folder must tolerate source frames living inside the
        target folder.
        """
        folder = tmp_path / "video"
        folder.mkdir()

        # Simulate the pipeline: frames live in a subdirectory of the
        # target folder, NOT in a sibling directory.
        internal_frames_dir = folder / "_extracted"
        internal_frames_dir.mkdir()
        frames = []
        for ts in [1.0, 30.0, 90.0]:
            p = internal_frames_dir / f"{seconds_to_timestamp(ts)}.jpg"
            p.write_bytes(b"fake jpeg data")
            frames.append(ExtractedFrame(ts, p))

        # This must NOT raise FileNotFoundError
        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            frames,
            transcript_source="test",
        )

        # All 3 frames must have been copied into batch-1
        batch_1 = folder / "batch-1"
        copied = [f.name for f in batch_1.iterdir() if f.suffix == ".jpg"]
        assert len(copied) == 3

    def test_stale_upload_to_claude_is_cleaned_up_on_reingest(self, tmp_path: Path):
        """
        Regression: when upgrading from an older version, re-ingesting
        should remove stale artifacts like UPLOAD-TO-CLAUDE.md.
        """
        folder = tmp_path / "video"
        folder.mkdir()
        # Simulate stale artifacts from a prior version
        (folder / "UPLOAD-TO-CLAUDE.md").write_text("old instructions")
        (folder / "transcript.txt").write_text("old transcript")
        (folder / "CLAUDE-INGESTION-PROMPT.md").write_text("old prompt")

        write_video_folder(
            folder,
            self._make_metadata(),
            self._make_segments(),
            self._make_frames(folder),
            transcript_source="test",
        )

        assert not (folder / "UPLOAD-TO-CLAUDE.md").exists()
        assert not (folder / "transcript.txt").exists()
        assert not (folder / "CLAUDE-INGESTION-PROMPT.md").exists()


# ----- Batching logic -----


class TestBatchPlanning:
    def _dummy_frames(self, count: int) -> list[ExtractedFrame]:
        """Generate N fake ExtractedFrames (paths don't need to exist for planning)."""
        return [
            ExtractedFrame(timestamp_seconds=float(i * 10), path=Path(f"/fake/{i:03d}.jpg"))
            for i in range(count)
        ]

    def test_small_video_is_single_batch(self):
        """15 frames fit in one batch (15 frames + 3 text files = 18 files)."""
        plan = plan_batches(self._dummy_frames(15), batch_size=18, attachment_limit=20)
        assert plan.batched is False  # batched means >1 batch, not "has batches"
        assert len(plan.frame_batches) == 1
        assert len(plan.frame_batches[0]) == 15

    def test_exactly_at_limit_is_single_batch(self):
        """
        first_batch_capacity = min(18,20) - 3 = 15 frames.
        So 15 frames = exactly one batch. 16 frames triggers a second batch.
        """
        plan = plan_batches(self._dummy_frames(15), batch_size=18, attachment_limit=20)
        assert plan.batched is False
        assert len(plan.frame_batches) == 1

    def test_over_limit_triggers_batching(self):
        """16 frames can't fit in batch-1 (cap: 15), so we get 2 batches."""
        plan = plan_batches(self._dummy_frames(16), batch_size=18, attachment_limit=20)
        assert plan.batched is True
        assert len(plan.frame_batches) == 2

    def test_60_frames_split_into_four_batches(self):
        # 60 frames, batch_size 18, 3 text files:
        # batch-1: 18 - 3 = 15 frames
        # batch-2, 3, 4: 18 frames each = 54
        # Total: 15 + 45 = 60
        plan = plan_batches(self._dummy_frames(60), batch_size=18, attachment_limit=20)
        assert plan.batched is True
        assert len(plan.frame_batches) == 4
        assert len(plan.frame_batches[0]) == 15
        assert len(plan.frame_batches[1]) == 18
        assert len(plan.frame_batches[2]) == 18
        assert len(plan.frame_batches[3]) == 9

    def test_frames_preserved_in_batching(self):
        """All original frames must be present exactly once across all batches."""
        frames = self._dummy_frames(60)
        plan = plan_batches(frames, batch_size=18, attachment_limit=20)
        all_batched = [f for batch in plan.frame_batches for f in batch]
        assert len(all_batched) == 60
        # Same timestamps in same order
        assert [f.timestamp_seconds for f in all_batched] == [
            f.timestamp_seconds for f in frames
        ]

    def test_batched_layout_creates_batch_folders(self, tmp_path: Path):
        """When a video triggers batching, batch-N folders are created."""
        # Create 25 fake frame files on disk so shutil.copy2 works
        frames_src = tmp_path / "src_frames"
        frames_src.mkdir()
        frames = []
        for i in range(25):
            p = frames_src / f"{i:03d}.jpg"
            p.write_bytes(b"fake")
            frames.append(ExtractedFrame(timestamp_seconds=float(i * 5), path=p))

        folder = tmp_path / "video"
        plan = write_video_folder(
            folder,
            VideoMetadata(
                video_id="x",
                title="Big Video",
                creator="Creator",
                duration_seconds=200,
                upload_date=None,
                description="",
                url="https://youtube.com/watch?v=x",
                thumbnail_url=None,
                has_captions=True,
                caption_languages=["en"],
            ),
            segments=[TranscriptSegment(0.0, 200.0, "content")],
            frames=frames,
            transcript_source="test",
        )
        assert plan.batched is True
        assert (folder / "batch-1").is_dir()
        assert (folder / "batch-2").is_dir()
        # Text files should be in batch-1
        assert (folder / "batch-1" / "transcript.md").exists()
        assert (folder / "batch-1" / "FRAMES-index.md").exists()
        assert (folder / "batch-1" / "ABOUT-this-video.md").exists()
        # START-HERE file lives ONLY in root (user drags this in first)
        assert (folder / "START-HERE-for-Claude.md").exists()
        assert not (folder / "batch-1" / "START-HERE-for-Claude.md").exists()
        # No top-level frames/ folder — only batch folders hold frames
        assert not (folder / "frames").exists()
        # No UPLOAD-TO-CLAUDE.md anywhere in the per-video folder
        assert not (folder / "UPLOAD-TO-CLAUDE.md").exists()

    def test_batched_ingestion_prompt_mentions_batches(self, tmp_path: Path):
        frames_src = tmp_path / "src_frames"
        frames_src.mkdir()
        frames = []
        for i in range(30):
            p = frames_src / f"{i:03d}.jpg"
            p.write_bytes(b"fake")
            frames.append(ExtractedFrame(timestamp_seconds=float(i * 5), path=p))

        folder = tmp_path / "video"
        write_video_folder(
            folder,
            VideoMetadata(
                video_id="x",
                title="Big Video",
                creator="Creator",
                duration_seconds=200,
                upload_date=None,
                description="",
                url="https://youtube.com/watch?v=x",
                thumbnail_url=None,
                has_captions=True,
                caption_languages=["en"],
            ),
            segments=[TranscriptSegment(0.0, 200.0, "content")],
            frames=frames,
            transcript_source="test",
        )
        prompt = (folder / "START-HERE-for-Claude.md").read_text()
        # Should contain batch orchestration instructions
        assert "batch 1" in prompt.lower()
        assert "IMPORTANT" in prompt
        assert "Received batch" in prompt

        # Regression test for 1.2.1 bug: when the user drags ONLY
        # START-HERE-for-Claude.md into a new chat, Claude used to reply
        # "Received batch 1 of N" even though no batch had been received.
        # 1.2.2 fixes this: the prompt now has explicit "STATE A" for
        # "just reading this file" and "STATE B" for "received a batch",
        # with "Ready for batch 1" as the correct response to STATE A.
        assert "STATE A" in prompt
        assert "STATE B" in prompt
        assert "Ready for batch 1" in prompt
        # The STATE A instruction must explicitly forbid saying "Received batch 1"
        assert 'Do NOT say "Received batch 1"' in prompt

    def test_prompt_requires_tool_read_and_forbids_frames_only(self, tmp_path: Path):
        """
        Regression test for the v2.1.0 field-testing bug: Claude claimed
        the transcript was missing when it had actually been uploaded as
        a fetchable attachment. v2.1.1 hardens the prompt with two
        explicit behaviors — Claude must tool-read the transcript before
        declaring it missing, and must never answer from frames alone.

        Both the single-batch and multi-batch prompts must contain the
        pinned phrases.
        """
        # Single-batch case
        frames_src_a = tmp_path / "src_small"
        frames_src_a.mkdir()
        small_frames = []
        for i in range(5):
            p = frames_src_a / f"{i:03d}.jpg"
            p.write_bytes(b"fake")
            small_frames.append(ExtractedFrame(timestamp_seconds=float(i * 5), path=p))
        folder_a = tmp_path / "single"
        write_video_folder(
            folder_a,
            VideoMetadata(
                video_id="s", title="Single", creator="Creator",
                duration_seconds=60, upload_date=None, description="",
                url="https://youtube.com/watch?v=s", thumbnail_url=None,
                has_captions=True, caption_languages=["en"],
            ),
            segments=[TranscriptSegment(0.0, 60.0, "content")],
            frames=small_frames,
            transcript_source="test",
        )
        single_prompt = (folder_a / "START-HERE-for-Claude.md").read_text(
            encoding="utf-8-sig"
        )
        assert "file-read tool" in single_prompt, \
            "single-batch prompt must tell Claude to use the file-read tool"
        assert "frames alone" in single_prompt, \
            "single-batch prompt must forbid answering from frames alone"
        # v2.1.2: ambiguity-flagging rule for pronoun / speaker attribution
        assert "pronoun" in single_prompt, \
            "single-batch prompt must address pronoun attribution ambiguity"
        assert "inference" in single_prompt, \
            "single-batch prompt must distinguish inferences from confirmed facts"

        # Multi-batch case
        frames_src_b = tmp_path / "src_big"
        frames_src_b.mkdir()
        big_frames = []
        for i in range(30):
            p = frames_src_b / f"{i:03d}.jpg"
            p.write_bytes(b"fake")
            big_frames.append(ExtractedFrame(timestamp_seconds=float(i * 5), path=p))
        folder_b = tmp_path / "multi"
        write_video_folder(
            folder_b,
            VideoMetadata(
                video_id="m", title="Multi", creator="Creator",
                duration_seconds=200, upload_date=None, description="",
                url="https://youtube.com/watch?v=m", thumbnail_url=None,
                has_captions=True, caption_languages=["en"],
            ),
            segments=[TranscriptSegment(0.0, 200.0, "content")],
            frames=big_frames,
            transcript_source="test",
        )
        multi_prompt = (folder_b / "START-HERE-for-Claude.md").read_text(
            encoding="utf-8-sig"
        )
        assert "file-read tool" in multi_prompt, \
            "multi-batch prompt must tell Claude to use the file-read tool"
        assert "frames alone" in multi_prompt, \
            "multi-batch prompt must forbid answering from frames alone"
        # v2.1.2: ambiguity-flagging rule for pronoun / speaker attribution
        assert "pronoun" in multi_prompt, \
            "multi-batch prompt must address pronoun attribution ambiguity"
        assert "inference" in multi_prompt, \
            "multi-batch prompt must distinguish inferences from confirmed facts"
        # The three-state structure from v1.2.2 must still be intact —
        # the new paragraphs are additions, not replacements.
        assert "STATE A" in multi_prompt
        assert "STATE B" in multi_prompt
        assert 'Do NOT say "Received batch 1"' in multi_prompt

    def test_batched_produces_correct_batch_folder_structure(self, tmp_path: Path):
        """
        Verify the batched layout: frames distributed correctly, no
        UPLOAD-TO-CLAUDE.md, START-HERE only in root.
        """
        frames_src = tmp_path / "src_frames"
        frames_src.mkdir()
        frames = []
        for i in range(40):
            p = frames_src / f"{i:03d}.jpg"
            p.write_bytes(b"fake")
            frames.append(ExtractedFrame(timestamp_seconds=float(i * 5), path=p))

        folder = tmp_path / "video"
        plan = write_video_folder(
            folder,
            VideoMetadata(
                video_id="x",
                title="Big Video",
                creator="Creator",
                duration_seconds=300,
                upload_date=None,
                description="",
                url="https://youtube.com/watch?v=x",
                thumbnail_url=None,
                has_captions=True,
                caption_languages=["en"],
            ),
            segments=[TranscriptSegment(0.0, 300.0, "content")],
            frames=frames,
            transcript_source="test",
        )
        # 40 frames: batch-1 gets 15, rest go into 18-frame batches
        # = batch-1 (15) + batch-2 (18) + batch-3 (7)
        assert len(plan.frame_batches) == 3
        for i in range(1, 4):
            assert (folder / f"batch-{i}").is_dir()

        # No UPLOAD-TO-CLAUDE.md anywhere
        assert not (folder / "UPLOAD-TO-CLAUDE.md").exists()
        # START-HERE only in root
        assert (folder / "START-HERE-for-Claude.md").exists()
        assert not (folder / "batch-1" / "START-HERE-for-Claude.md").exists()

        # All 40 source frames copied into the batch folders exactly once
        copied = set()
        for i in range(1, 4):
            for f in (folder / f"batch-{i}").iterdir():
                if f.suffix == ".jpg":
                    copied.add(f.name)
        assert len(copied) == 40


# ----- Library index -----


class TestLibraryIndex:
    @pytest.fixture(autouse=True)
    def isolated_library(self, tmp_path: Path, monkeypatch):
        """Isolate the library to a tmp folder for each test."""
        monkeypatch.setenv("VIDEO_INGEST_LIBRARY", str(tmp_path / "library"))
        yield

    def test_update_creates_index(self):
        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-20",
                title="Test",
                creator="Me",
                duration="1:00",
                folder_name="2026-04-20_test_by-me",
                video_url="https://youtube.com/watch?v=x",
            )
        )
        from video_ingest.paths import library_index_path
        content = library_index_path().read_text()
        assert "Test" in content
        assert "2026-04-20_test_by-me" in content

    def test_update_replaces_existing_entry(self):
        entry = LibraryEntry(
            ingest_date="2026-04-20",
            title="Test",
            creator="Me",
            duration="1:00",
            folder_name="2026-04-20_test_by-me",
            video_url="https://youtube.com/watch?v=x",
        )
        update_library_index(entry)
        # Update with new info for the same folder
        entry2 = LibraryEntry(
            ingest_date="2026-04-21",
            title="Test Updated",
            creator="Me",
            duration="2:00",
            folder_name="2026-04-20_test_by-me",
            video_url="https://youtube.com/watch?v=x",
        )
        update_library_index(entry2)

        from video_ingest.paths import library_index_path
        content = library_index_path().read_text()
        # Count table rows mentioning this folder — the folder name appears
        # twice per row (link text + URL), so count line occurrences instead.
        rows_for_folder = [
            line for line in content.splitlines()
            if "2026-04-20_test_by-me" in line and line.startswith("|")
        ]
        assert len(rows_for_folder) == 1
        assert "Test Updated" in rows_for_folder[0]
        assert "| Test |" not in content

    def test_multiple_entries_sort_by_date_desc(self):
        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-18",
                title="Older",
                creator="A",
                duration="1:00",
                folder_name="2026-04-18_older_by-a",
                video_url="https://youtube.com/watch?v=a",
            )
        )
        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-20",
                title="Newer",
                creator="B",
                duration="1:00",
                folder_name="2026-04-20_newer_by-b",
                video_url="https://youtube.com/watch?v=b",
            )
        )
        from video_ingest.paths import library_index_path
        content = library_index_path().read_text()
        # Newer should appear before Older in the file
        assert content.index("Newer") < content.index("Older")

    def test_library_readme_written(self):
        write_library_readme()
        from video_ingest.paths import library_root
        assert (library_root() / "README-LIBRARY.md").exists()

    def test_reconcile_removes_entries_for_deleted_folders(self):
        """
        When a video folder is deleted manually, calling reconcile should
        prune its row from library.md.
        """
        from video_ingest.library import reconcile_library_index
        from video_ingest.paths import library_index_path, library_root

        # Create two library entries and corresponding folders on disk
        for name in ("2026-04-20_kept_by-a", "2026-04-20_deleted_by-b"):
            (library_root() / name).mkdir(parents=True, exist_ok=True)

        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-20",
                title="Kept",
                creator="A",
                duration="1:00",
                folder_name="2026-04-20_kept_by-a",
                video_url="https://youtube.com/watch?v=a",
            )
        )
        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-20",
                title="Deleted",
                creator="B",
                duration="1:00",
                folder_name="2026-04-20_deleted_by-b",
                video_url="https://youtube.com/watch?v=b",
            )
        )

        # Now delete one of the folders (simulate user cleaning up)
        import shutil
        shutil.rmtree(library_root() / "2026-04-20_deleted_by-b")

        kept, removed = reconcile_library_index()
        assert kept == 1
        assert removed == 1

        content = library_index_path().read_text()
        assert "Kept" in content
        assert "Deleted" not in content

    def test_reconcile_on_clean_library_is_noop(self):
        """Reconciling when every folder exists should return (kept, 0)."""
        from video_ingest.library import reconcile_library_index
        from video_ingest.paths import library_root

        (library_root() / "2026-04-20_x_by-y").mkdir(parents=True, exist_ok=True)
        update_library_index(
            LibraryEntry(
                ingest_date="2026-04-20",
                title="X",
                creator="Y",
                duration="1:00",
                folder_name="2026-04-20_x_by-y",
                video_url="https://youtube.com/watch?v=x",
            )
        )

        kept, removed = reconcile_library_index()
        assert removed == 0
        assert kept == 1

    def test_reconcile_with_no_index_is_noop(self):
        """Reconciling before library.md exists should not crash."""
        from video_ingest.library import reconcile_library_index
        kept, removed = reconcile_library_index()
        assert kept == 0
        assert removed == 0

    def test_reconcile_prunes_deleted_folders(self, tmp_path: Path, monkeypatch):
        """Rows for folders that no longer exist on disk should be removed."""
        from video_ingest.library import reconcile_library_index
        from video_ingest.paths import ensure_library_root, library_index_path

        # Add two entries. Create folder for only one.
        update_library_index(LibraryEntry(
            ingest_date="2026-04-20", title="Kept", creator="A",
            duration="1:00", folder_name="2026-04-20_kept_by-a",
            video_url="https://youtube.com/watch?v=1",
        ))
        update_library_index(LibraryEntry(
            ingest_date="2026-04-20", title="Deleted", creator="B",
            duration="2:00", folder_name="2026-04-20_deleted_by-b",
            video_url="https://youtube.com/watch?v=2",
        ))

        # Create only the first folder on disk
        root = ensure_library_root()
        (root / "2026-04-20_kept_by-a").mkdir(parents=True)

        kept, removed = reconcile_library_index()
        assert kept == 1
        assert removed == 1

        content = library_index_path().read_text()
        assert "Kept" in content
        assert "Deleted" not in content

    def test_reconcile_is_idempotent(self, tmp_path: Path, monkeypatch):
        """Running reconcile a second time with no changes on disk is a noop."""
        from video_ingest.library import reconcile_library_index
        from video_ingest.paths import ensure_library_root

        update_library_index(LibraryEntry(
            ingest_date="2026-04-20", title="Kept", creator="A",
            duration="1:00", folder_name="2026-04-20_kept_by-a",
            video_url="https://youtube.com/watch?v=1",
        ))
        (ensure_library_root() / "2026-04-20_kept_by-a").mkdir(parents=True)

        reconcile_library_index()  # first call
        kept, removed = reconcile_library_index()  # second call
        assert kept == 1
        assert removed == 0


# ----- Library location resolution (v2.1.2) -----


class TestLibraryRootResolution:
    """
    v2.1.2: library_root() supports a settings-based override. Priority:
      1. VIDEO_INGEST_LIBRARY env var
      2. library_location in settings.json
      3. Default ~/Documents/claude-video-library/
    """

    def test_env_var_wins_over_settings_and_default(
        self, tmp_path: Path, monkeypatch
    ):
        from video_ingest import paths

        env_dir = tmp_path / "from-env"
        settings_dir = tmp_path / "from-settings"
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            json.dumps({"library_location": str(settings_dir)}),
            encoding="utf-8",
        )
        monkeypatch.setenv("VIDEO_INGEST_LIBRARY", str(env_dir))
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: config_dir)

        resolved = paths.library_root()
        assert resolved == env_dir.expanduser().resolve()

    def test_settings_used_when_no_env_var(self, tmp_path: Path, monkeypatch):
        from video_ingest import paths

        settings_dir = tmp_path / "custom-library"
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            json.dumps({"library_location": str(settings_dir)}),
            encoding="utf-8",
        )
        monkeypatch.delenv("VIDEO_INGEST_LIBRARY", raising=False)
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: config_dir)

        resolved = paths.library_root()
        assert resolved == settings_dir.expanduser().resolve()

    def test_default_when_neither_set(self, tmp_path: Path, monkeypatch):
        from video_ingest import paths

        empty_config = tmp_path / "empty-config"
        empty_config.mkdir()
        monkeypatch.delenv("VIDEO_INGEST_LIBRARY", raising=False)
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: empty_config)

        resolved = paths.library_root()
        assert resolved == Path.home() / "Documents" / "claude-video-library"

    def test_empty_library_location_field_falls_through(
        self, tmp_path: Path, monkeypatch
    ):
        """An explicit empty string in settings means 'use the default'."""
        from video_ingest import paths

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            json.dumps({"library_location": ""}),
            encoding="utf-8",
        )
        monkeypatch.delenv("VIDEO_INGEST_LIBRARY", raising=False)
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: config_dir)

        resolved = paths.library_root()
        assert resolved == Path.home() / "Documents" / "claude-video-library"

    def test_corrupt_settings_falls_through_silently(
        self, tmp_path: Path, monkeypatch
    ):
        from video_ingest import paths

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )
        monkeypatch.delenv("VIDEO_INGEST_LIBRARY", raising=False)
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: config_dir)

        resolved = paths.library_root()
        assert resolved == Path.home() / "Documents" / "claude-video-library"

    def test_not_cached(self, tmp_path: Path, monkeypatch):
        """
        Changing the settings file between two calls to library_root()
        must produce different results — paths.py must not cache.
        """
        from video_ingest import paths

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.delenv("VIDEO_INGEST_LIBRARY", raising=False)
        monkeypatch.setattr(paths, "_settings_config_dir", lambda: config_dir)

        (config_dir / "settings.json").write_text(
            json.dumps({"library_location": str(first)}), encoding="utf-8"
        )
        resolved1 = paths.library_root()

        (config_dir / "settings.json").write_text(
            json.dumps({"library_location": str(second)}), encoding="utf-8"
        )
        resolved2 = paths.library_root()

        assert resolved1 == first.expanduser().resolve()
        assert resolved2 == second.expanduser().resolve()
        assert resolved1 != resolved2


class TestGuiSettingsRoundTrip:
    """v2.1.2: library_location round-trips through GuiSettings JSON."""

    def test_library_location_default_is_empty(self):
        from video_ingest.gui.settings import GuiSettings
        assert GuiSettings().library_location == ""

    def test_library_location_round_trip(self):
        from video_ingest.gui.settings import GuiSettings

        s = GuiSettings(library_location="/some/path/to/library")
        reloaded = GuiSettings.from_json(s.to_json())
        assert reloaded.library_location == "/some/path/to/library"

    def test_old_settings_without_library_location_still_load(self):
        """A settings.json written by v2.1.1 (no library_location field)
        must still load with the new v2.1.2 dataclass, defaulting the
        new field to empty."""
        from video_ingest.gui.settings import GuiSettings

        old_json = json.dumps({
            "max_frames": 60,
            "whisper_model": "base",
            "use_whisper_fallback": True,
        })
        loaded = GuiSettings.from_json(old_json)
        assert loaded.library_location == ""
        assert loaded.max_frames == 60


# ----- Error types -----


class TestErrors:
    def test_error_carries_fix_list(self):
        err = VideoIngestError(
            what="Something broke",
            fix=["Try this", "Then this"],
        )
        assert err.what == "Something broke"
        assert err.fix == ["Try this", "Then this"]

    def test_error_with_string_fix_normalized_to_list(self):
        err = VideoIngestError(what="x", fix="Single fix")
        assert err.fix == ["Single fix"]

    def test_error_with_no_fix(self):
        err = VideoIngestError(what="x")
        assert err.fix == []


# ----- Doctor -----


class TestDoctor:
    def test_run_checks_returns_all_checks(self):
        from video_ingest.doctor import run_checks

        results = run_checks()
        names = [r.name for r in results]
        assert "Python version" in names
        assert "ffmpeg" in names
        assert "yt-dlp (Python package)" in names


# ----- CLI argument parsing -----


class TestCLI:
    def test_version_flag(self):
        from video_ingest.cli import build_parser

        parser = build_parser()
        # --version exits, so this is mostly a smoke test that the parser builds
        assert parser is not None

    def test_parses_url_argument(self):
        from video_ingest.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["https://youtube.com/watch?v=abc"])
        assert args.url == "https://youtube.com/watch?v=abc"
        assert args.max_frames == 60
        assert args.no_whisper is False

    def test_max_frames_override(self):
        from video_ingest.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["--max-frames", "100", "https://x.com"])
        assert args.max_frames == 100

    def test_doctor_flag(self):
        from video_ingest.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["--doctor"])
        assert args.doctor is True
