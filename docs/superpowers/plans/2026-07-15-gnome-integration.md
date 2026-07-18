# GNOME Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Top-bar recording-status indicator and a Ctrl+Alt+M launch keybind for GNOME, mirroring the repo's existing Hyprland/Waybar integration.

**Architecture:** A standalone AppIndicator script in a new `gnome/` directory polls the `.status` file the app already writes (same file the Waybar module reads). Pure parsing/state logic sits at module top level (stdlib only, pytest-covered); GTK/AppIndicator code lives inside `main()` so importing the module never requires PyGObject. An idempotent `install.sh` generates the `~/.local/bin/meeting-notes` launcher, registers the GNOME keybind via gsettings, and installs an autostart entry.

**Tech Stack:** Python 3 (system `/usr/bin/python3` with PyGObject + AyatanaAppIndicator3, both preinstalled), bash, gsettings, kitty, pytest (repo venv).

## Global Constraints

- The indicator runs on `/usr/bin/python3`, never the repo venv. No new pip or apt dependencies.
- `import gi` must NOT appear at module top level of the indicator — CI has no PyGObject; tests import the module.
- Tests run with the repo venv: `venv/bin/python -m pytest`.
- `.status` file format is frozen: shell-sourceable `KEY="value"` lines, keys `STATUS` (`idle|recording|processing`), optional `TITLE`, `DURATION`. Written to the repo root.
- App-running detection matches the Waybar module exactly: `pgrep -f "python.*run.py"`.
- Keybind is exactly `<Control><Alt>m`; kitty window class is exactly `meeting-notes`.
- The repo root is auto-detected as the parent of the `gnome/` directory (same pattern as `hyprland/waybar-module.sh`).
- `docs/` is gitignored in this repo — never `git add` anything under `docs/`.

---

### Task 1: Status parsing and state resolution (pure logic, TDD)

**Files:**
- Create: `gnome/meeting-notes-indicator.py` (pure-logic half only; `main()` comes in Task 2)
- Test: `tests/test_gnome_indicator.py`

**Interfaces:**
- Produces: `parse_status_file(text: str) -> dict[str, str]` — parses `KEY="value"` lines, ignores malformed lines.
- Produces: `resolve_state(app_running: bool, status: dict) -> tuple[str, str, str]` — returns `(state, label, title)`; `state` is one of `"not_running" | "ready" | "recording" | "processing"`.
- Produces: `ICONS: dict[str, str]` mapping each state to a GTK icon name.
- Task 2's `main()` and Task 3's installer rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gnome_indicator.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_gnome_indicator.py -q`
Expected: FAIL at module load — `FileNotFoundError` for `gnome/meeting-notes-indicator.py` (collection error counts; every test errors).

- [ ] **Step 3: Write the pure-logic half of the indicator**

Create `gnome/meeting-notes-indicator.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_gnome_indicator.py -q`
Expected: `12 passed`

- [ ] **Step 5: Run the full suite to check nothing broke**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all tests pass (127 before this plan + 12 new).

- [ ] **Step 6: Commit**

```bash
git add gnome/meeting-notes-indicator.py tests/test_gnome_indicator.py
git commit -m "gnome: add indicator status parsing + state resolution (pure logic)"
```

---

### Task 2: AppIndicator runtime (`main()`)

**Files:**
- Modify: `gnome/meeting-notes-indicator.py` (append `main()` and entry point)

**Interfaces:**
- Consumes: `parse_status_file`, `resolve_state`, `read_status`, `app_running`, `repo_root`, `ICONS`, `POLL_SECONDS` from Task 1 (same module).
- Produces: script runnable as `/usr/bin/python3 gnome/meeting-notes-indicator.py`; menu's "Open Meeting Notes" invokes `~/.local/bin/meeting-notes` (created by Task 3).

- [ ] **Step 1: Append the GTK runtime to the script**

Append to `gnome/meeting-notes-indicator.py`:

```python
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
```

- [ ] **Step 2: Verify the module still imports without GTK and tests still pass**

Run: `venv/bin/python -m pytest tests/test_gnome_indicator.py -q`
Expected: `12 passed` (the venv has no PyGObject, so this also proves `import gi` stayed out of module scope).

- [ ] **Step 3: Smoke-test the indicator on the live session**

Run: `timeout 5 /usr/bin/python3 gnome/meeting-notes-indicator.py; echo "exit=$?"`
Expected: `exit=124` (killed by timeout after running cleanly for 5s — no traceback). An icon briefly appears in the GNOME top bar.

- [ ] **Step 4: Verify state switching with a synthetic .status file**

The app isn't running, so `not_running` wins regardless of the file — this check exercises the recording path by faking the process check:

```bash
printf 'STATUS="recording"\nTITLE="fake"\nDURATION="12:34"\n' > .status.test
/usr/bin/python3 - <<'EOF'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("mn", "gnome/meeting-notes-indicator.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.resolve_state(True, m.read_status(Path(".status.test"))))
EOF
rm .status.test
```

Expected output: `('recording', '12:34', 'Recording: fake')`

- [ ] **Step 5: Commit**

```bash
git add gnome/meeting-notes-indicator.py
git commit -m "gnome: add AppIndicator runtime with 2s status polling"
```

---

### Task 3: Installer — launcher, keybind, autostart

**Files:**
- Create: `gnome/install.sh`

**Interfaces:**
- Consumes: `gnome/meeting-notes-indicator.py` from Tasks 1–2.
- Produces: `~/.local/bin/meeting-notes` (launcher invoked by the keybind and the indicator menu), gsettings entry at `/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/meeting-notes/`, `~/.config/autostart/meeting-notes-indicator.desktop`.

- [ ] **Step 1: Write the installer**

Create `gnome/install.sh` (mark executable):

```bash
#!/bin/bash
# Meeting Notes — GNOME integration installer.
# Idempotent: safe to re-run after moving the repo or changing the keybind.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
LAUNCHER="$HOME/.local/bin/meeting-notes"
AUTOSTART="$HOME/.config/autostart/meeting-notes-indicator.desktop"
KEY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/meeting-notes/"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys"

command -v gsettings >/dev/null || { echo "error: gsettings not found — is this GNOME?" >&2; exit 1; }
command -v kitty >/dev/null || { echo "error: kitty not found" >&2; exit 1; }

# 1. Launcher ---------------------------------------------------------------
mkdir -p "$(dirname "$LAUNCHER")"
cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Launch Meeting Notes TUI (generated by gnome/install.sh — do not edit).
if pgrep -f "python.*run.py" >/dev/null; then
    notify-send "Meeting Notes" "Already running" -i audio-input-microphone || true
    exit 0
fi
if ! command -v kitty >/dev/null; then
    notify-send "Meeting Notes" "kitty not found — cannot launch" || true
    exit 1
fi
exec kitty --class meeting-notes --directory "$REPO" "$REPO/venv/bin/python" "$REPO/run.py"
EOF
chmod 0755 "$LAUNCHER"
echo "installed launcher: $LAUNCHER"

# 2. Keybind (Ctrl+Alt+M) ---------------------------------------------------
# Append our path to the custom-keybindings list without clobbering others.
CURRENT="$(gsettings get "$SCHEMA" custom-keybindings)"
NEW_LIST="$(/usr/bin/python3 - "$CURRENT" "$KEY_PATH" <<'PYEOF'
import ast, sys
raw, path = sys.argv[1], sys.argv[2]
raw = raw.removeprefix("@as").strip()
lst = ast.literal_eval(raw) if raw and raw != "[]" else []
if path not in lst:
    lst.append(path)
print(lst)
PYEOF
)"
gsettings set "$SCHEMA" custom-keybindings "$NEW_LIST"
gsettings set "$SCHEMA.custom-keybinding:$KEY_PATH" name "Meeting Notes"
gsettings set "$SCHEMA.custom-keybinding:$KEY_PATH" command "$LAUNCHER"
gsettings set "$SCHEMA.custom-keybinding:$KEY_PATH" binding "<Control><Alt>m"
echo "registered keybind: Ctrl+Alt+M -> $LAUNCHER"

# 3. Autostart entry for the indicator --------------------------------------
mkdir -p "$(dirname "$AUTOSTART")"
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Meeting Notes Indicator
Comment=Recording status in the GNOME top bar
Exec=/usr/bin/python3 $REPO/gnome/meeting-notes-indicator.py
X-GNOME-Autostart-enabled=true
EOF
echo "installed autostart: $AUTOSTART"

# 4. Start the indicator now (if not already running) ------------------------
if ! pgrep -f "meeting-notes-indicator.py" >/dev/null; then
    nohup /usr/bin/python3 "$REPO/gnome/meeting-notes-indicator.py" >/dev/null 2>&1 &
    echo "started indicator (pid $!)"
else
    echo "indicator already running"
fi

echo "done."
```

Then: `chmod +x gnome/install.sh`

- [ ] **Step 2: Run the installer**

Run: `gnome/install.sh`
Expected output (paths expanded):
```
installed launcher: /home/harry/.local/bin/meeting-notes
registered keybind: Ctrl+Alt+M -> /home/harry/.local/bin/meeting-notes
installed autostart: /home/harry/.config/autostart/meeting-notes-indicator.desktop
started indicator (pid NNNNN)
done.
```

- [ ] **Step 3: Verify each artifact**

```bash
test -x ~/.local/bin/meeting-notes && echo launcher-ok
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings | grep -o meeting-notes && echo list-ok
gsettings get "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/meeting-notes/" binding
pgrep -f meeting-notes-indicator.py && echo indicator-running
```
Expected: `launcher-ok`, `list-ok`, `'<Control><Alt>m'`, a PID + `indicator-running`.

- [ ] **Step 4: Verify idempotency**

Run: `gnome/install.sh` again, then:
```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings | grep -c meeting-notes
```
Expected: installer prints `indicator already running`; grep prints `1` (no duplicate list entry).

- [ ] **Step 5: Commit**

```bash
git add gnome/install.sh
git commit -m "gnome: add idempotent installer (launcher, Ctrl+Alt+M keybind, autostart)"
```

---

### Task 4: README section

**Files:**
- Modify: `README.md` (insert a "GNOME Integration" section immediately after the "Hyprland/Waybar Integration" section, i.e. after the existing `### Keybinding` block and before `## Audio Configuration`)

**Interfaces:**
- Consumes: `gnome/install.sh` from Task 3 (documented command).

- [ ] **Step 1: Insert the section**

Add to `README.md` right before the `## Audio Configuration` heading:

````markdown
## GNOME Integration

For GNOME (tested on GNOME Shell 47 / Ubuntu, Wayland), a top-bar status
indicator plus a global launch keybind. Requires the AppIndicator shell
extension (`ubuntu-appindicators`, enabled by default on Ubuntu) and `kitty`.

```bash
gnome/install.sh
```

This installs:

- **`~/.local/bin/meeting-notes`** — launches the TUI in kitty. If the app
  is already running you get a notification instead of a second instance.
- **Ctrl+Alt+M** — global keybind for the launcher (GNOME custom shortcut).
- **Top-bar indicator** — autostarts at login, polls the same `.status`
  file as the Waybar module: muted mic = app not running, mic = ready,
  record icon + live `05:42` timer = recording, sync icon = processing.
  Right-click for *Open Meeting Notes*, *Open notes folder*, *Quit*.

Re-running the installer is safe (it updates paths in place — run it again
after moving the repo).

**Uninstall:**

```bash
rm -f ~/.local/bin/meeting-notes ~/.config/autostart/meeting-notes-indicator.desktop
pkill -f meeting-notes-indicator.py
# Remove the keybind entry:
gsettings reset-recursively "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/meeting-notes/"
```
(then remove the `/…/meeting-notes/` path from
`gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings`
via GNOME Settings → Keyboard, or leave it — GNOME ignores dangling entries).
````

- [ ] **Step 2: Sanity-check the README renders**

Run: `grep -n "GNOME Integration" README.md`
Expected: one hit, located between the Hyprland section and `## Audio Configuration`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document GNOME integration (indicator, installer, keybind)"
```

---

### Task 5: Live end-to-end verification

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1–4.

- [ ] **Step 1: Full test suite + fresh indicator state**

```bash
venv/bin/python -m pytest tests/ -q
pgrep -f meeting-notes-indicator.py
```
Expected: all tests pass; indicator PID printed.

- [ ] **Step 2: Launcher behaviour**

```bash
~/.local/bin/meeting-notes &
sleep 3
pgrep -f "python.*run.py" && echo app-started
~/.local/bin/meeting-notes   # second invocation
```
Expected: a kitty window opens with the TUI; `app-started`; the second call returns immediately and pops an "Already running" notification instead of a second window.

- [ ] **Step 3: Indicator reflects a real recording**

With the TUI open from Step 2, ask the user to press `r` to start a short test recording, confirm the top-bar icon switches to the record icon with a counting timer, press `Esc`/stop, then quit the app. Confirm the icon returns to muted-mic (not running).

- [ ] **Step 4: Keybind**

Ask the user to press **Ctrl+Alt+M** and confirm the TUI opens (this cannot be simulated programmatically on Wayland).

- [ ] **Step 5: Report results**

No commit — report what passed/failed to the user, including anything requiring their manual confirmation.
