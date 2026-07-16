"""Shared clipboard helper: wl-copy (Wayland) with xclip/xsel fallback."""

import shutil
import subprocess

from .logger import get_logger

logger = get_logger(__name__)

_TOOLS = [
    ("wl-copy", ["wl-copy"]),                            # Wayland
    ("xclip", ["xclip", "-selection", "clipboard"]),     # X11
    ("xsel", ["xsel", "--clipboard"]),                   # X11
]


def copy_text_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to the system clipboard.

    Returns (ok, message) — message is user-facing for a notify().
    """
    for tool, cmd in _TOOLS:
        if shutil.which(tool):
            try:
                process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                process.communicate(text.encode())
                return True, "✓ Copied to clipboard"
            except Exception as e:  # noqa: BLE001 - report, try no further
                logger.error(f"clipboard copy via {tool} failed: {e}")
                return False, f"Failed to copy: {e}"
    return False, "Install wl-clipboard (Wayland) or xclip/xsel (X11)"
