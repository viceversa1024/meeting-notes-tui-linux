"""Tests for the voice-tag command (grow the voice library from labeled
transcript lines). See docs/superpowers/specs/2026-07-16-voice-tag-library-design.md.
"""

import shutil
import wave

import pytest

from meeting_notes.voice_tag import best_utterance, parse_labeled_lines


TRANSCRIPT = """Meeting: test
Date: July 16, 2026
Recording: {recording}

────────────────────────────

**[00:05] A:** short.

**[00:07] Alex:** my own voice, ignore me here.

**[00:12] A:** this is a much longer utterance that runs for a while.

**[00:26]** unlabeled line.

**[00:30] A:** trailing line with no successor.
"""


def test_parse_labeled_lines_extracts_speaker_and_start():
    lines = parse_labeled_lines(TRANSCRIPT.format(recording=""))
    assert [(ln.speaker, ln.start) for ln in lines] == [
        ("A", 5.0), ("Alex", 7.0), ("A", 12.0), ("A", 30.0),
    ]
    # gap to the next line (any speaker) estimates the duration
    assert lines[0].duration == 2.0
    assert lines[2].duration == 14.0
    assert lines[3].duration is None  # last line: unknown


def test_parse_handles_hours_timestamps():
    lines = parse_labeled_lines("**[01:02:03] B:** hi\n\n**[01:02:10] B:** yo\n")
    assert lines[0].start == 3723.0
    assert lines[0].duration == 7.0


def test_best_utterance_prefers_longest_gap():
    lines = parse_labeled_lines(TRANSCRIPT.format(recording=""))
    best = best_utterance(lines, "A")
    assert best.start == 12.0  # the 8s utterance, not the 2s one


def test_best_utterance_skips_too_short_and_unknown_label():
    lines = parse_labeled_lines("**[00:01] A:** blip\n\n**[00:02] B:** x\n")
    assert best_utterance(lines, "A") is None  # 1s gap < 2s minimum
    assert best_utterance(lines, "Z") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_tag_voice_end_to_end(tmp_path):
    from meeting_notes.voice_tag import tag_voice

    recording = tmp_path / "meeting.wav"
    with wave.open(str(recording), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000 * 30)

    transcript = tmp_path / "meeting.txt"
    transcript.write_text(TRANSCRIPT.format(recording=recording.name))

    voices = tmp_path / "voices"
    out = tag_voice(
        transcript_path=str(transcript),
        label="A",
        name="Blake",
        recording_path=str(recording),
        voices_dir=str(voices),
    )

    assert out == str(voices / "Blake.wav")
    with wave.open(out, "rb") as w:
        clip_seconds = w.getnframes() / w.getframerate()
    # API hard limit is 10.0s and ffmpeg -t can land a hair over the ask,
    # so the cutter must stay safely under it (live 400: "must be between
    # 1.2 and 10.0 seconds").
    assert 2.0 <= clip_seconds <= 9.5


def test_tag_voice_missing_recording_is_clear_error(tmp_path):
    from meeting_notes.voice_tag import tag_voice

    transcript = tmp_path / "meeting.txt"
    transcript.write_text(TRANSCRIPT.format(recording=""))

    with pytest.raises(FileNotFoundError, match="[Rr]ecording"):
        tag_voice(
            transcript_path=str(transcript),
            label="A",
            name="Blake",
            recording_path=None,
            voices_dir=str(tmp_path / "voices"),
        )
