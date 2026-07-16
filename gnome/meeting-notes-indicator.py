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


def main() -> int:
    # GTK imports stay inside main() — see module docstring.
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import GLib, Gtk
    from gi.repository import AyatanaAppIndicator3 as AppIndicator

    repo = repo_root()
    status_path = repo / ".status"
    launcher = Path.home() / ".local" / "bin" / "meeting-notes"

    indicator = AppIndicator.Indicator.new(
        "meeting-notes",
        ICONS["not_running"],
        AppIndicator.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def on_open(_item):
        subprocess.Popen([str(launcher)])

    def on_notes(_item):
        subprocess.Popen(["xdg-open", str(repo / "notes")])

    menu = Gtk.Menu()
    for label, handler in (
        ("Open Meeting Notes", on_open),
        ("Open notes folder", on_notes),
        ("Quit indicator", lambda _item: Gtk.main_quit()),
    ):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", handler)
        menu.append(item)
    menu.show_all()
    indicator.set_menu(menu)

    def refresh() -> bool:
        state, label, title = resolve_state(app_running(), read_status(status_path))
        indicator.set_icon_full(ICONS[state], title)
        # Guide width "88:88" stops the bar jittering as digits change.
        indicator.set_label(label, "88:88")
        indicator.set_title(title)
        return True  # keep the GLib timer alive

    refresh()
    GLib.timeout_add_seconds(POLL_SECONDS, refresh)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
