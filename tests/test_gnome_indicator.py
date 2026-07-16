"""Tests for the pure logic half of the GNOME AppIndicator script.

The script filename has dashes (matching hyprland/waybar-module.sh naming),
so we load it via importlib. Importing must never require PyGObject —
GTK imports live inside main().
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parent.parent / "gnome" / "meeting-notes-indicator.py"
_spec = importlib.util.spec_from_file_location("mn_indicator", _MODULE_PATH)
indicator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(indicator)


# ----- parse_status_file -----

def test_parse_recording_status():
    text = 'STATUS="recording"\nTITLE="standup"\nDURATION="05:42"\n'
    assert indicator.parse_status_file(text) == {
        "STATUS": "recording",
        "TITLE": "standup",
        "DURATION": "05:42",
    }

def test_parse_preserves_spaces_in_values():
    text = 'STATUS="recording"\nTITLE="convo with helena about walking"\n'
    parsed = indicator.parse_status_file(text)
    assert parsed["TITLE"] == "convo with helena about walking"

def test_parse_ignores_garbage_lines():
    text = 'STATUS="idle"\nnot a kv line\n= broken\nKEY_NO_QUOTES=x\n'
    assert indicator.parse_status_file(text) == {"STATUS": "idle"}

def test_parse_empty_text():
    assert indicator.parse_status_file("") == {}


# ----- resolve_state -----

def test_resolve_app_not_running_wins_over_status():
    state, label, title = indicator.resolve_state(False, {"STATUS": "recording"})
    assert state == "not_running"
    assert label == ""

def test_resolve_running_no_status_file_is_ready():
    state, _, _ = indicator.resolve_state(True, {})
    assert state == "ready"

def test_resolve_idle_is_ready():
    state, _, _ = indicator.resolve_state(True, {"STATUS": "idle"})
    assert state == "ready"

def test_resolve_recording_shows_duration_and_title():
    state, label, title = indicator.resolve_state(
        True, {"STATUS": "recording", "DURATION": "05:42", "TITLE": "standup"}
    )
    assert state == "recording"
    assert label == "05:42"
    assert "standup" in title

def test_resolve_recording_defaults():
    state, label, title = indicator.resolve_state(True, {"STATUS": "recording"})
    assert state == "recording"
    assert label == "00:00"
    assert "Meeting" in title

def test_resolve_processing():
    state, label, _ = indicator.resolve_state(True, {"STATUS": "processing"})
    assert state == "processing"
    assert label == ""

def test_resolve_unknown_status_degrades_to_ready():
    state, _, _ = indicator.resolve_state(True, {"STATUS": "banana"})
    assert state == "ready"

def test_icons_cover_all_states():
    for state in ("not_running", "ready", "recording", "processing"):
        assert state in indicator.ICONS
