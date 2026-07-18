"""Publish meeting notes as unlisted HTML pages (S3+CloudFront).

The share URL for a published note lives in the note's own YAML
frontmatter (`share_url:`) — it survives renames and travels with
Obsidian exports. Frontmatter edits here must preserve every other key
and the body byte-for-byte.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Optional

from .logger import get_logger

logger = get_logger(__name__)

_SHARE_URL_RE = re.compile(r'^share_url:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)


class UploadError(Exception):
    """User-presentable publish/unpublish failure."""


def generate_slug() -> str:
    """128-bit unguessable URL slug."""
    return secrets.token_urlsafe(16)


def split_frontmatter(content: str) -> tuple[Optional[str], str]:
    """Split a note into (frontmatter_inner, body).

    Returns (None, content) when there is no frontmatter. The parts
    reassemble losslessly as f"---{fm}---{body}".
    """
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            return parts[1], parts[2]
    return None, content


def read_share_url(note_path: Path) -> Optional[str]:
    fm, _ = split_frontmatter(note_path.read_text())
    if fm is None:
        return None
    match = _SHARE_URL_RE.search(fm)
    return match.group(1) if match else None


def write_share_url(note_path: Path, url: str) -> None:
    content = note_path.read_text()
    fm, body = split_frontmatter(content)
    if fm is None:
        raise UploadError("Note has no frontmatter — can't record share_url")
    # If a share_url line exists, replace it; otherwise append.
    if _SHARE_URL_RE.search(fm):
        new_fm = re.sub(
            r'^share_url:[^\n]*',
            f'share_url: "{url}"',
            fm,
            flags=re.MULTILINE
        )
    else:
        new_fm = fm.rstrip('\n') + f'\nshare_url: "{url}"\n'
    note_path.write_text(f"---{new_fm}---{body}")


def remove_share_url(note_path: Path) -> None:
    content = note_path.read_text()
    fm, body = split_frontmatter(content)
    if fm is None:
        return
    # Remove the whole line including its newline so surrounding lines —
    # even intentional blank ones — stay byte-identical.
    new_fm = re.sub(r'^share_url:[^\n]*\n', '', fm, flags=re.MULTILINE)
    note_path.write_text(f"---{new_fm}---{body}")
