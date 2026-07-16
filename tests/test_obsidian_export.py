"""Tests for the Obsidian vault export of meeting summary notes.

Notes are copied (best-effort) into `obsidian_dir` after being saved.
See docs/superpowers/specs/2026-07-16-obsidian-export-design.md.
"""

from meeting_notes.note_maker import NoteMaker


def _make_note(nm):
    return nm.create_note(
        transcript_text="hello world this is a meeting",
        formatted_transcript="**[00:00]** hello world this is a meeting",
        duration=42.0,
        title="obsidian test",
    )


def test_note_copied_into_obsidian_dir(tmp_path):
    vault = tmp_path / "vault" / "meetings"
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
        obsidian_dir=str(vault),
    )
    note_path, _, _ = _make_note(nm)

    note_name = note_path.rsplit("/", 1)[-1]
    vault_copy = vault / note_name
    assert vault_copy.is_file()
    assert vault_copy.read_text() == (tmp_path / "notes" / note_name).read_text()


def test_obsidian_dir_created_on_demand(tmp_path):
    """The vault subfolder may not exist yet — create it at copy time."""
    vault = tmp_path / "not" / "yet" / "there"
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
        obsidian_dir=str(vault),
    )
    # Not created at init: the feature must not touch the vault until a note
    # is actually saved.
    assert not vault.exists()
    _make_note(nm)
    assert vault.is_dir() and list(vault.glob("*.md"))


def test_default_is_off(tmp_path):
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
    )
    _make_note(nm)

    # Nothing outside notes/ + transcripts/ was written
    created = {p.name for p in tmp_path.iterdir()}
    assert created == {"notes", "transcripts"}


def test_uncreatable_obsidian_dir_does_not_break_note_saving(tmp_path):
    """A file sitting where the vault dir should be must not lose the note."""
    blocker = tmp_path / "vault"
    blocker.write_text("i am a file, not a directory")

    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
        obsidian_dir=str(blocker),
    )
    note_path, transcript_path, _ = _make_note(nm)

    from pathlib import Path
    assert Path(note_path).is_file()
    assert Path(transcript_path).is_file()


def test_obsidian_dir_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    nm = NoteMaker(
        output_dir=str(tmp_path / "notes"),
        transcripts_dir=str(tmp_path / "transcripts"),
        ai_provider="none",
        obsidian_dir="~/vault/meetings",
    )
    _make_note(nm)
    assert list((tmp_path / "vault" / "meetings").glob("*.md"))
