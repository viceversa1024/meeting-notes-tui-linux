"""GUI editors must be launched directly, not wrapped in a terminal window."""

import pytest

textual = pytest.importorskip("textual", reason="app import needs textual")

from meeting_notes.app import MeetingNotesApp  # noqa: E402


@pytest.mark.parametrize("editor,expected", [
    ("gnome-text-editor", True),
    ("gedit", True),
    ("code", True),
    ("nvim", False),
    ("vim", False),
    ("nano", False),
    ("emacs -nw", False),
    ("", False),
])
def test_is_gui_editor(editor, expected):
    assert MeetingNotesApp._is_gui_editor(editor) is expected
