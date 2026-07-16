"""Frontmatter enrichment: participants, transcription/summary models.

The Obsidian properties block should carry useful provenance — who was in
the meeting (best guess), which model transcribed it, which model wrote
the summary.
"""

import re

from meeting_notes.note_maker import NoteMaker


FORMATTED = (
    "**[00:05] Alex:** hello there\n\n"
    "**[00:08] B:** hi\n\n"
    "**[00:12] Blake:** morning\n"
)


def _frontmatter(note_path):
    text = open(note_path).read()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "note must start with a frontmatter block"
    return m.group(1)


def _make_note(nm, **kwargs):
    return nm.create_note(
        transcript_text="hello there hi morning",
        formatted_transcript=FORMATTED,
        duration=42.0,
        title="frontmatter test",
        **kwargs,
    )[0]


def test_participants_from_named_speakers(tmp_path):
    """Without AI, named (non-letter) transcript speakers are the guess."""
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
    )
    fm = _frontmatter(_make_note(nm))
    assert 'participants: ["Alex", "Blake"]' in fm  # "B" is not a name


def test_transcription_model_from_metadata(tmp_path):
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
    )
    fm = _frontmatter(_make_note(
        nm, metadata={"transcription_model": "gpt-4o-transcribe-diarize",
                      "recording_file": "2026-07-16.wav"},
    ))
    assert 'transcription_model: "gpt-4o-transcribe-diarize"' in fm
    assert 'recording_file: "2026-07-16.wav"' in fm


def test_summary_model_recorded(tmp_path, monkeypatch):
    """The summarizer's human-readable model name lands in frontmatter."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="openai",
        ai_model="best",
        api_key="sk-test",
    )

    # Don't hit the network: replace the summarizer with a stub.
    from meeting_notes.ai_summarizer import MeetingSummary

    class _Stub:
        def summarize(self, text, user_notes=""):
            return MeetingSummary(
                overview="o", key_points=[], action_items=[], decisions=[],
                participants=["Alex", "Casey [maybe]", "Blake"],
            )

    nm.summarizer = _Stub()

    fm = _frontmatter(_make_note(nm))
    assert 'summary_model: "GPT-5.6 Sol"' in fm
    # Union of AI participants (junk like "[...]" filtered) and named
    # transcript speakers, no duplicates.
    assert 'participants: ["Alex", "Blake"]' in fm


def test_no_summary_model_when_ai_disabled(tmp_path):
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
    )
    fm = _frontmatter(_make_note(nm))
    assert "summary_model" not in fm
