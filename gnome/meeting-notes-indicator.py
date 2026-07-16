#!/usr/bin/env python3
"""Meeting Notes — GNOME top-bar status indicator.

Reads the same repo-root .status file as hyprland/waybar-module.sh and shows
recording state in the GNOME top bar via AppIndicator (needs the
ubuntu-appindicators shell extension, enabled by default on Ubuntu).

Runs on the SYSTEM python3 (PyGObject is an apt package, not in the venv):
    /usr/bin/python3 gnome/meeting-notes-indicator.py

GTK imports live inside main() so this module imports cleanly without
PyGObject — tests exercise the pure functions below.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

POLL_SECONDS = 2

ICONS = {
    "not_running": "microphone-sensitivity-muted-symbolic",
    "ready": "audio-input-microphone-symbolic",
    "recording": "media-record-symbolic",
    "processing": "emblem-synchronizing-symbolic",
}


def repo_root() -> Path:
    """The repo is the parent of the gnome/ directory this script lives in."""
    return Path(__file__).resolve().parent.parent


def parse_status_file(text: str) -> dict[str, str]:
    """Parse shell-sourceable KEY="value" lines; ignore anything malformed."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not (value.startswith('"') and value.endswith('"') and len(value) >= 2):
            continue
        result[key] = value[1:-1]
    return result


def resolve_state(app_running: bool, status: dict) -> tuple[str, str, str]:
    """Map (app running?, parsed .status) to (state, top-bar label, title).

    Mirrors hyprland/waybar-module.sh: a dead app wins over any stale file;
    anything unrecognised degrades to "ready" rather than erroring.
    """
    if not app_running:
        return ("not_running", "", "Meeting Notes (not running)")
    s = status.get("STATUS", "")
    if s == "recording":
        duration = status.get("DURATION") or "00:00"
        title = status.get("TITLE") or "Meeting"
        return ("recording", duration, f"Recording: {title}")
    if s == "processing":
        return ("processing", "", "Processing recording…")
    return ("ready", "", "Meeting Notes (ready)")


def app_running() -> bool:
    """Same detection as the Waybar module."""
    try:
        return subprocess.run(
            ["pgrep", "-f", "python.*run.py"], capture_output=True
        ).returncode == 0
    except Exception:
        return False


def read_status(path: Path) -> dict[str, str]:
    try:
        return parse_status_file(path.read_text())
    except Exception:
        return {}
