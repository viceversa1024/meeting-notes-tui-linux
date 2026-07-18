# Notes Upload (notes.harrywaterman.com) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a meeting's summary note from the TUI as an unlisted HTML page at `https://notes.harrywaterman.com/n/<slug>`, copy the link, and unpublish it later.

**Architecture:** A new `meeting_notes/uploader.py` renders the note markdown (frontmatter/footer stripped) to a self-contained HTML page and PUTs it to a private S3 bucket served through CloudFront OAC. The share URL is stored in the note's own YAML frontmatter (`share_url:`). TUI keys `u`/`U` publish/unpublish via background workers. `cloud/setup.sh` provisions the AWS side.

**Tech Stack:** Python 3.10+, Textual, boto3 + `markdown` (new optional extra `[upload]`), AWS CLI (bash) for infra.

**Spec:** `docs/superpowers/specs/2026-07-17-notes-upload-design.md`

## Global Constraints

- All test/lint commands run from repo root with the project venv: `venv/bin/python -m pytest ...`, `venv/bin/ruff check meeting_notes/ tests/`.
- CI must stay lightweight: **never** import `boto3` at module load in tests (inject a fake into `sys.modules`, like the fake `faster_whisper` in `tests/test_transcriber_device.py`). `markdown` IS allowed in CI (pure-Python, lightweight).
- New test files must be added to the explicit pytest file list in `.github/workflows/ci.yml` or CI won't run them.
- Object key format: `n/<slug>`, slug from `secrets.token_urlsafe(16)`. URL format: `<upload_base_url>/n/<slug>` with `upload_base_url` default `https://notes.harrywaterman.com`.
- Ordering guarantees: frontmatter `share_url` is written **only after** a successful S3 PUT; on unpublish the S3 delete happens **before** frontmatter removal; a missing-key error on delete counts as success.
- Frontmatter edits must preserve every other key and the body byte-for-byte.
- Upload failures are non-fatal notifications; missing boto3 produces an install hint, never a traceback.
- Config file is written with mode 0600 (existing behavior — don't break it).
- Ruff ruleset is `E9,F,B` with the ignores in `pyproject.toml`; new code must pass `venv/bin/ruff check meeting_notes/ tests/`.

---

### Task 1: Config fields + validation

**Files:**
- Modify: `meeting_notes/config.py` (dataclass fields after `obsidian_dir`, ~line 70; validation at end of `validate_config`, before `return True, None` ~line 270)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `AppConfig.upload_bucket: str = ""`, `AppConfig.upload_region: str = "us-east-1"`, `AppConfig.upload_base_url: str = "https://notes.harrywaterman.com"`. Empty `upload_bucket` means the feature is off. `validate_config` rejects a non-`https://` `upload_base_url` only when `upload_bucket` is set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
# --- notes upload config ---------------------------------------------------

def test_upload_fields_default_off():
    config = AppConfig()
    assert config.upload_bucket == ""
    assert config.upload_region == "us-east-1"
    assert config.upload_base_url == "https://notes.harrywaterman.com"


def test_upload_disabled_skips_base_url_validation():
    # Feature off: even a garbage base URL must not fail validation.
    config = AppConfig(upload_bucket="", upload_base_url="not a url")
    valid, error = validate_config(config)
    assert valid, error


def test_upload_enabled_rejects_bad_base_url():
    config = AppConfig(upload_bucket="my-bucket", upload_base_url="ftp://nope")
    valid, error = validate_config(config)
    assert not valid
    assert "upload_base_url" in error


def test_upload_enabled_accepts_https_base_url():
    config = AppConfig(upload_bucket="my-bucket")
    valid, error = validate_config(config)
    assert valid, error
```

(`AppConfig` and `validate_config` are already imported at the top of `tests/test_config.py`; if not, add `from meeting_notes.config import AppConfig, validate_config`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_config.py -v -k upload`
Expected: FAIL — `TypeError: AppConfig.__init__() got an unexpected keyword argument 'upload_bucket'`

- [ ] **Step 3: Implement**

In `meeting_notes/config.py`, add fields to the `AppConfig` dataclass directly after `obsidian_dir: str = ""`:

```python
    # Unlisted web publishing (S3+CloudFront, see cloud/setup.sh). Empty
    # upload_bucket disables the feature. AWS credentials come from the
    # standard boto3 chain (env vars / ~/.aws), never from this file.
    upload_bucket: str = ""
    upload_region: str = "us-east-1"
    upload_base_url: str = "https://notes.harrywaterman.com"
```

In `validate_config`, immediately before the final `return True, None`:

```python
    # Validate upload settings (only when publishing is enabled)
    if config.upload_bucket:
        import re
        if not re.match(r'^https://[^/]+', config.upload_base_url or ""):
            return False, (
                f"Invalid upload_base_url: {config.upload_base_url!r}. "
                "Must be an https:// URL like https://notes.harrywaterman.com"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_config.py -v`
Expected: all PASS (including pre-existing tests)

- [ ] **Step 5: Lint and commit**

```bash
venv/bin/ruff check meeting_notes/ tests/
git add meeting_notes/config.py tests/test_config.py
git commit -m "feat: add upload_bucket/upload_region/upload_base_url config"
```

---

### Task 2: Uploader module — slug + frontmatter share_url helpers

**Files:**
- Create: `meeting_notes/uploader.py`
- Create: `tests/test_uploader.py`
- Modify: `.github/workflows/ci.yml` (append `tests/test_uploader.py` to the pytest file list)

**Interfaces:**
- Produces (module-level functions in `meeting_notes.uploader`):
  - `generate_slug() -> str`
  - `split_frontmatter(content: str) -> tuple[Optional[str], str]` — `(frontmatter_inner, body)`; `(None, content)` when no frontmatter
  - `read_share_url(note_path: Path) -> Optional[str]`
  - `write_share_url(note_path: Path, url: str) -> None` — raises `UploadError` if the note has no frontmatter
  - `remove_share_url(note_path: Path) -> None`
  - `class UploadError(Exception)` — message is user-presentable

- [ ] **Step 1: Write the failing tests**

Create `tests/test_uploader.py`:

```python
"""Tests for meeting_notes.uploader.

boto3 is never imported for real: the fake_boto3 fixture (used from Task 4
onward) injects a fake module into sys.modules, mirroring the fake
faster_whisper pattern in tests/test_transcriber_device.py. uploader.py
imports boto3 lazily inside NoteUploader._client() precisely so this works.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting_notes.uploader import (
    UploadError,
    generate_slug,
    read_share_url,
    remove_share_url,
    split_frontmatter,
    write_share_url,
)

SAMPLE_NOTE = '''---
title: "Weekly Sync"
date: 2026-07-17
time: "14:00"
duration_seconds: 1800
word_count: 4213
tags: [meeting, auto-generated]
recording_file: "rec.wav"
transcript_file: "tr.md"
---

# Weekly Sync

**Date:** July 17, 2026 at 02:00 PM  
**Duration:** 30m  
**Words:** 4,213

## Summary

We discussed the *roadmap*.

---

*View full transcript: Press 't' to view transcript*  
*Generated by Meeting Notes v0.3.0 on 2026-07-17 at 15:00:00*
'''


@pytest.fixture
def note(tmp_path) -> Path:
    p = tmp_path / "2026-07-17-weekly-sync.md"
    p.write_text(SAMPLE_NOTE)
    return p


# --- slug -------------------------------------------------------------------

def test_slug_is_long_and_urlsafe():
    slugs = {generate_slug() for _ in range(50)}
    assert len(slugs) == 50  # no collisions in a small sample
    for slug in slugs:
        assert len(slug) >= 20  # token_urlsafe(16) -> ~22 chars
        assert all(c.isalnum() or c in "-_" for c in slug)


# --- frontmatter helpers ----------------------------------------------------

def test_split_frontmatter_roundtrip():
    fm, body = split_frontmatter(SAMPLE_NOTE)
    assert 'title: "Weekly Sync"' in fm
    assert body.lstrip().startswith("# Weekly Sync")
    # Reassembly must be lossless
    assert f"---{fm}---{body}" == SAMPLE_NOTE


def test_split_frontmatter_none_when_missing():
    fm, body = split_frontmatter("# Just a heading\n")
    assert fm is None
    assert body == "# Just a heading\n"


def test_share_url_roundtrip_preserves_everything_else(note):
    assert read_share_url(note) is None
    url = "https://notes.harrywaterman.com/n/abc123XYZ_-abc123XYZ_-"
    write_share_url(note, url)
    assert read_share_url(note) == url
    remove_share_url(note)
    assert read_share_url(note) is None
    assert note.read_text() == SAMPLE_NOTE  # byte-for-byte restore


def test_write_share_url_requires_frontmatter(tmp_path):
    bare = tmp_path / "bare.md"
    bare.write_text("# No frontmatter here\n")
    with pytest.raises(UploadError):
        write_share_url(bare, "https://example.com/n/x")


def test_remove_share_url_noop_when_absent(note):
    remove_share_url(note)  # must not raise
    assert note.read_text() == SAMPLE_NOTE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meeting_notes.uploader'`

- [ ] **Step 3: Implement**

Create `meeting_notes/uploader.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v`
Expected: all PASS. If `test_share_url_roundtrip_preserves_everything_else` fails on the byte-for-byte assert, debug the write/remove pair until reassembly is exact — that guarantee is a spec requirement, not a nicety.

- [ ] **Step 5: Add the test file to CI**

In `.github/workflows/ci.yml`, append to the pytest file list (after `tests/test_meeting_routing.py`, keeping the backslash continuation on the previous line):

```yaml
            tests/test_meeting_routing.py \
            tests/test_uploader.py
```

- [ ] **Step 6: Lint and commit**

```bash
venv/bin/ruff check meeting_notes/ tests/
git add meeting_notes/uploader.py tests/test_uploader.py .github/workflows/ci.yml
git commit -m "feat: uploader module with slug + share_url frontmatter helpers"
```

---

### Task 3: HTML renderer + `[upload]` extra

**Files:**
- Modify: `meeting_notes/uploader.py` (append)
- Modify: `pyproject.toml` (optional-dependencies)
- Modify: `.github/workflows/ci.yml` (add `markdown` to the pip install list)
- Test: `tests/test_uploader.py` (append)

**Interfaces:**
- Consumes: `split_frontmatter`, `UploadError` from Task 2.
- Produces: `render_note_html(note_path: Path) -> str` — full standalone HTML document; raises `UploadError` with an install hint if the `markdown` package is missing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_uploader.py`:

```python
# --- rendering --------------------------------------------------------------

def test_render_strips_frontmatter_and_footer(note):
    from meeting_notes.uploader import render_note_html
    html = render_note_html(note)
    assert "share_url" not in html
    assert "recording_file" not in html          # no frontmatter leaked
    assert "View full transcript" not in html    # local-only footer gone
    assert "Generated by Meeting Notes" not in html


def test_render_produces_selfcontained_noindex_html(note):
    from meeting_notes.uploader import render_note_html
    html = render_note_html(note)
    assert html.startswith("<!DOCTYPE html>")
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert "<title>Weekly Sync</title>" in html
    assert "<h1>Weekly Sync</h1>" in html        # markdown converted
    assert "<em>roadmap</em>" in html
    # Self-contained: no external fetches of any kind
    assert 'src="http' not in html and 'href="http' not in html
    assert "<link" not in html and "<script" not in html


def test_render_without_frontmatter_still_works(tmp_path):
    from meeting_notes.uploader import render_note_html
    bare = tmp_path / "bare.md"
    bare.write_text("# Standalone\n\nBody text.\n")
    html = render_note_html(bare)
    assert "<h1>Standalone</h1>" in html
    assert "<title>Meeting note</title>" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v -k render`
Expected: FAIL — `ImportError: cannot import name 'render_note_html'`

- [ ] **Step 3: Add the `[upload]` extra and install locally**

In `pyproject.toml`, after the `openrouter = [...]` line add:

```toml
upload = ["boto3>=1.34", "markdown>=3.5"]
```

and extend the `all` extra to:

```toml
all = [
    "openai>=1.0.0",
    "anthropic>=0.18.0",
    "openrouter>=0.1.0",
    "boto3>=1.34",
    "markdown>=3.5",
]
```

Install into the venv: `venv/bin/pip install boto3 markdown`

- [ ] **Step 4: Implement the renderer**

Append to `meeting_notes/uploader.py`:

```python
_TITLE_RE = re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE)

# Everything a page needs is inline: no external CSS/JS/fonts/images, so a
# note renders even if it's the only object anyone ever fetches.
_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
body {{ max-width: 42rem; margin: 2rem auto; padding: 0 1rem;
       font-family: Georgia, 'Times New Roman', serif;
       line-height: 1.6; color: #222; background: #fff; }}
h1, h2, h3 {{ font-family: Helvetica, Arial, sans-serif; line-height: 1.25; }}
code, pre {{ background: #f4f4f4; padding: 0.1em 0.3em; }}
a {{ color: #0645ad; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #121212; color: #ddd; }}
  code, pre {{ background: #2a2a2a; }}
  a {{ color: #8ab4f8; }}
}}
</style>
</head>
<body>
{content}
</body>
</html>
"""


def _strip_footer(body: str) -> str:
    """Drop the local-only footer NoteMaker appends to every note."""
    lines = [
        line for line in body.splitlines()
        if not line.startswith('*View full transcript')
        and not line.startswith('*Generated by Meeting Notes')
    ]
    while lines and lines[-1].strip() == '':
        lines.pop()
    if lines and lines[-1].strip() == '---':  # footer's separator rule
        lines.pop()
    return '\n'.join(lines).strip() + '\n'


def render_note_html(note_path: Path) -> str:
    """Render a note's markdown body as a standalone, noindex HTML page."""
    try:
        import markdown as md
    except ImportError:
        raise UploadError(
            "The 'markdown' package is required for publishing — "
            "run: pip install 'meeting-notes[upload]'"
        )
    fm, body = split_frontmatter(note_path.read_text())
    title = "Meeting note"
    if fm is not None:
        match = _TITLE_RE.search(fm)
        if match:
            title = match.group(1)
    content = md.markdown(_strip_footer(body), extensions=['extra'])
    return _PAGE_TEMPLATE.format(title=title, content=content)
```

- [ ] **Step 5: Update CI's install step**

In `.github/workflows/ci.yml`, add `markdown \` to the `pip install` list (after `pyyaml \`):

```yaml
          pip install \
            pyyaml \
            markdown \
            openai \
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v`
Expected: all PASS

- [ ] **Step 7: Lint and commit**

```bash
venv/bin/ruff check meeting_notes/ tests/
git add meeting_notes/uploader.py tests/test_uploader.py pyproject.toml .github/workflows/ci.yml
git commit -m "feat: render notes to self-contained noindex HTML; add [upload] extra"
```

---

### Task 4: NoteUploader — upload/unpublish over S3

**Files:**
- Modify: `meeting_notes/uploader.py` (append)
- Test: `tests/test_uploader.py` (append)

**Interfaces:**
- Consumes: Task 1 config fields; Task 2 helpers; Task 3 `render_note_html`.
- Produces: `class NoteUploader` with `__init__(self, config: AppConfig)`, `upload(self, note_path: Path) -> tuple[str, bool]` (returns `(url, already_published)`), `unpublish(self, note_path: Path) -> str` (returns the now-dead URL). Both raise `UploadError` on failure. boto3 is imported lazily inside `_client()` only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_uploader.py` (also add `import sys` and `import types` to the file's imports):

```python
# --- NoteUploader over a fake boto3 ----------------------------------------

class FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.delete_calls = []
        self.put_error = None
        self.delete_error = None

    def put_object(self, **kwargs):
        if self.put_error:
            raise self.put_error
        self.put_calls.append(kwargs)

    def delete_object(self, **kwargs):
        if self.delete_error:
            raise self.delete_error
        self.delete_calls.append(kwargs)


@pytest.fixture
def fake_s3(monkeypatch) -> FakeS3Client:
    """Install a fake boto3 whose client() returns a controllable S3 stub."""
    client = FakeS3Client()
    fake_mod = types.ModuleType("boto3")
    fake_mod.client = lambda *args, **kwargs: client
    monkeypatch.setitem(sys.modules, "boto3", fake_mod)
    return client


@pytest.fixture
def upload_config():
    from meeting_notes.config import AppConfig
    return AppConfig(upload_bucket="test-bucket",
                     upload_base_url="https://notes.harrywaterman.com")


def test_upload_puts_html_and_writes_frontmatter(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader
    url, already = NoteUploader(upload_config).upload(note)
    assert not already
    assert len(fake_s3.put_calls) == 1
    call = fake_s3.put_calls[0]
    assert call["Bucket"] == "test-bucket"
    assert call["Key"].startswith("n/")
    assert call["ContentType"] == "text/html; charset=utf-8"
    assert b"<!DOCTYPE html>" in call["Body"]
    slug = call["Key"][len("n/"):]
    assert url == f"https://notes.harrywaterman.com/n/{slug}"
    assert read_share_url(note) == url


def test_upload_already_published_returns_existing_without_put(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader
    write_share_url(note, "https://notes.harrywaterman.com/n/existing-slug")
    url, already = NoteUploader(upload_config).upload(note)
    assert already
    assert url == "https://notes.harrywaterman.com/n/existing-slug"
    assert fake_s3.put_calls == []


def test_upload_failure_leaves_note_untouched(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader, UploadError
    fake_s3.put_error = RuntimeError("network down")
    with pytest.raises(UploadError):
        NoteUploader(upload_config).upload(note)
    assert read_share_url(note) is None
    assert note.read_text() == SAMPLE_NOTE


def test_unpublish_deletes_then_clears_frontmatter(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader
    write_share_url(note, "https://notes.harrywaterman.com/n/doomed-slug")
    NoteUploader(upload_config).unpublish(note)
    assert fake_s3.delete_calls == [{"Bucket": "test-bucket", "Key": "n/doomed-slug"}]
    assert read_share_url(note) is None
    assert note.read_text() == SAMPLE_NOTE


def test_unpublish_failure_keeps_share_url(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader, UploadError
    write_share_url(note, "https://notes.harrywaterman.com/n/sticky-slug")
    fake_s3.delete_error = RuntimeError("access denied")
    with pytest.raises(UploadError):
        NoteUploader(upload_config).unpublish(note)
    # Failed delete must leave the link recorded (still live at origin)
    assert read_share_url(note) == "https://notes.harrywaterman.com/n/sticky-slug"


def test_unpublish_tolerates_missing_key(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader
    write_share_url(note, "https://notes.harrywaterman.com/n/gone-slug")
    fake_s3.delete_error = RuntimeError("An error occurred (NoSuchKey) when calling DeleteObject")
    NoteUploader(upload_config).unpublish(note)  # must not raise
    assert read_share_url(note) is None


def test_unpublish_unpublished_note_raises(note, fake_s3, upload_config):
    from meeting_notes.uploader import NoteUploader, UploadError
    with pytest.raises(UploadError):
        NoteUploader(upload_config).unpublish(note)


def test_missing_boto3_gives_install_hint(note, upload_config, monkeypatch):
    from meeting_notes.uploader import NoteUploader, UploadError
    import builtins
    real_import = builtins.__import__

    def no_boto3(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    with pytest.raises(UploadError, match=r"meeting-notes\[upload\]"):
        NoteUploader(upload_config).upload(note)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v -k "uploader or upload or unpublish or boto3"`
Expected: FAIL — `ImportError: cannot import name 'NoteUploader'`

- [ ] **Step 3: Implement**

Append to `meeting_notes/uploader.py` (add `from .config import AppConfig` to the module imports):

```python
class NoteUploader:
    """Publish/unpublish a note against the configured S3 bucket."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.base_url = config.upload_base_url.rstrip('/')

    def _client(self):
        try:
            import boto3  # lazy: keeps boto3 optional and out of CI
        except ImportError:
            raise UploadError(
                "boto3 not installed — run: pip install 'meeting-notes[upload]'"
            )
        return boto3.client("s3", region_name=self.config.upload_region)

    def upload(self, note_path: Path) -> tuple[str, bool]:
        """Publish. Returns (url, already_published).

        share_url is written only after a successful PUT, so a failed
        upload leaves the note byte-identical.
        """
        existing = read_share_url(note_path)
        if existing:
            return existing, True
        html = render_note_html(note_path)
        slug = generate_slug()
        client = self._client()
        try:
            client.put_object(
                Bucket=self.config.upload_bucket,
                Key=f"n/{slug}",
                Body=html.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
            )
        except UploadError:
            raise
        except Exception as e:
            logger.error(f"Publish failed for {note_path.name}: {e}", exc_info=True)
            raise UploadError(f"Upload failed: {e}")
        url = f"{self.base_url}/n/{slug}"
        write_share_url(note_path, url)
        logger.info(f"Published {note_path.name} -> {url}")
        return url, False

    def unpublish(self, note_path: Path) -> str:
        """Delete from S3, then clear share_url. Returns the dead URL.

        S3 delete goes first so a failure leaves the link recorded (it is
        still live at the origin). A missing key counts as success.
        """
        url = read_share_url(note_path)
        if not url:
            raise UploadError("Note is not published")
        slug = url.rsplit('/n/', 1)[-1]
        client = self._client()
        try:
            client.delete_object(Bucket=self.config.upload_bucket, Key=f"n/{slug}")
        except Exception as e:
            if "NoSuchKey" not in str(e) and "404" not in str(e):
                logger.error(f"Unpublish failed for {note_path.name}: {e}", exc_info=True)
                raise UploadError(f"Unpublish failed: {e}")
        remove_share_url(note_path)
        logger.info(f"Unpublished {note_path.name} ({url})")
        return url
```

- [ ] **Step 4: Run the full test file**

Run: `venv/bin/python -m pytest tests/test_uploader.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
venv/bin/ruff check meeting_notes/ tests/
git add meeting_notes/uploader.py tests/test_uploader.py
git commit -m "feat: NoteUploader publish/unpublish over S3"
```

---

### Task 5: TUI integration — `u`/`U` keys, confirm modal, workers

**Files:**
- Modify: `meeting_notes/app.py`:
  - `BINDINGS` list (~line 733): two new bindings
  - New `ConfirmUnpublishScreen` class directly after `ConfirmDeleteScreen` (~line 563)
  - New actions/workers/helpers after `handle_delete_confirmation` (~line 1850)

**Interfaces:**
- Consumes: `NoteUploader`, `UploadError`, `read_share_url` (Task 4); `copy_text_to_clipboard(text) -> tuple[bool, str]` from `meeting_notes.clipboard`; existing `NoteViewer` (`viewer.current_note: Optional[Path]`, `viewer.show_note(path)`), `self.push_screen`, `@work(thread=True)` + `self.call_from_thread` patterns.
- Produces: `action_publish_note`, `action_unpublish_note` bound to `u`/`U`.

- [ ] **Step 1: Add bindings**

In `BINDINGS` (after the `Binding("T", ...)` line):

```python
        Binding("u", "publish_note", "Publish", show=True),
        Binding("U", "unpublish_note", "Unpublish", show=False),
```

- [ ] **Step 2: Add the confirmation modal**

Directly after `ConfirmDeleteScreen` (after line 562):

```python
class ConfirmUnpublishScreen(ModalScreen):
    """Confirm removing a published note from the web."""

    CSS = """
    ConfirmUnpublishScreen {
        align: center middle;
    }

    #unpublish-dialog {
        width: 60;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }

    #unpublish-message {
        text-align: center;
        margin: 1 0;
        color: $text;
    }

    #unpublish-buttons {
        width: 100%;
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    """

    def __init__(self, meeting_title: str, **kwargs):
        super().__init__(**kwargs)
        self.meeting_title = meeting_title

    def compose(self) -> ComposeResult:
        with Container(id="unpublish-dialog"):
            yield Static("🔗 Unpublish note?", id="unpublish-title")
            yield Static(
                f'"{self.meeting_title}"\n\nThe share link stops working immediately.',
                id="unpublish-message",
            )
            with Horizontal(id="unpublish-buttons"):
                yield Button("Cancel", variant="primary", id="cancel-button", classes="confirm-button")
                yield Button("Unpublish", variant="warning", id="unpublish-button", classes="confirm-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "unpublish-button")
```

- [ ] **Step 3: Add actions, workers, and helpers**

After `handle_delete_confirmation` in `MeetingNotesApp`:

```python
    def action_publish_note(self) -> None:
        """Publish the selected note as an unlisted web page (or re-copy its link)."""
        viewer = self.query_one("#note-viewer", NoteViewer)
        if not viewer.current_note:
            self.notify("No note selected", severity="warning")
            return
        if not self.config.upload_bucket:
            self.notify(
                "Uploads not configured — set upload_bucket in config.yaml (see cloud/setup.sh)",
                severity="warning",
            )
            return
        from meeting_notes.uploader import read_share_url
        existing = read_share_url(viewer.current_note)
        if existing:
            self._copy_share_url(existing, already=True)
            return
        self.notify("Publishing note...", severity="information")
        self.publish_note_worker(str(viewer.current_note))

    def _copy_share_url(self, url: str, already: bool = False) -> None:
        from meeting_notes.clipboard import copy_text_to_clipboard
        try:
            ok, _ = copy_text_to_clipboard(url)
        except Exception:
            ok = False
        prefix = "Already published" if already else "✓ Published"
        # If the clipboard is unavailable, surface the URL itself.
        suffix = " — link copied" if ok else f" — {url}"
        self.notify(f"{prefix}{suffix}", severity="information", timeout=10)

    def _refresh_current_note(self) -> None:
        viewer = self.query_one("#note-viewer", NoteViewer)
        if viewer.current_note:
            viewer.show_note(viewer.current_note)

    @work(thread=True)
    def publish_note_worker(self, note_path: str) -> None:
        from meeting_notes.uploader import NoteUploader, UploadError
        try:
            url, already = NoteUploader(self.config).upload(Path(note_path))
        except UploadError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        except Exception as e:
            self.call_from_thread(self.notify, f"Upload failed: {e}", severity="error")
            return
        self.call_from_thread(self._copy_share_url, url, already)
        self.call_from_thread(self._refresh_current_note)

    def action_unpublish_note(self) -> None:
        """Unpublish the selected note after confirmation."""
        viewer = self.query_one("#note-viewer", NoteViewer)
        if not viewer.current_note:
            self.notify("No note selected", severity="warning")
            return
        from meeting_notes.uploader import read_share_url
        if not read_share_url(viewer.current_note):
            self.notify("Note is not published", severity="warning")
            return
        self.push_screen(
            ConfirmUnpublishScreen(viewer.current_note.stem),
            self.handle_unpublish_confirmation,
        )

    def handle_unpublish_confirmation(self, confirmed: Optional[bool]) -> None:
        if confirmed is True:
            viewer = self.query_one("#note-viewer", NoteViewer)
            if viewer.current_note:
                self.unpublish_note_worker(str(viewer.current_note))

    @work(thread=True)
    def unpublish_note_worker(self, note_path: str) -> None:
        from meeting_notes.uploader import NoteUploader, UploadError
        try:
            NoteUploader(self.config).unpublish(Path(note_path))
        except UploadError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        except Exception as e:
            self.call_from_thread(self.notify, f"Unpublish failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, "✓ Unpublished — link is dead", severity="information")
        self.call_from_thread(self._refresh_current_note)
```

- [ ] **Step 4: Verify — lint, full local suite, headless smoke**

```bash
venv/bin/ruff check meeting_notes/ tests/
venv/bin/python -m pytest
```

Expected: ruff clean; full suite PASS (this machine has faster-whisper, so `test_textual_smoke.py` runs and exercises app startup — it will catch binding/CSS syntax errors).

Then manual check with a real note but no AWS config (feature-off path):

```bash
venv/bin/python run.py --dev
```

- Select a note, press `u` → expect "Uploads not configured — set upload_bucket in config.yaml" (unless config already has a bucket).
- Press `U` → expect "Note is not published".
- Quit with `q`.

- [ ] **Step 5: Commit**

```bash
git add meeting_notes/app.py
git commit -m "feat: publish/unpublish notes from the TUI (u/U)"
```

---

### Task 6: `cloud/setup.sh` + site assets

**Files:**
- Create: `cloud/setup.sh` (mode 755)
- Create: `cloud/site/404.html`
- Create: `cloud/site/robots.txt`

**Interfaces:**
- Consumes: nothing from the app. Produces the AWS resources the app's config points at (`upload_bucket`). Two-phase by design: first run creates bucket+cert and prints the ACM validation CNAME; a re-run after validation creates the CloudFront distribution and prints the final `notes` CNAME.
- Note: the distribution uses the **CachingDisabled** managed policy so unpublish is instant (no stale cached copies); traffic is personal-scale so origin hits are irrelevant.

- [ ] **Step 1: Create the site assets**

`cloud/site/404.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex, nofollow">
<title>Not found</title>
<style>
body { max-width: 42rem; margin: 4rem auto; padding: 0 1rem;
       font-family: Georgia, serif; color: #222; }
@media (prefers-color-scheme: dark) { body { background: #121212; color: #ddd; } }
</style>
</head>
<body>
<h1>Nothing here</h1>
<p>This link doesn't point to a note. It may have been unpublished.</p>
</body>
</html>
```

`cloud/site/robots.txt`:

```
User-agent: *
Disallow: /
```

- [ ] **Step 2: Write `cloud/setup.sh`**

```bash
#!/bin/bash
# Meeting Notes — notes.harrywaterman.com infrastructure (S3 + CloudFront).
# Idempotent: safe to re-run. Two-phase:
#   run 1: creates bucket + requests the ACM cert, prints the validation
#          CNAME to add at Hover, exits.
#   run 2 (after the cert validates, ~5-30 min): creates the OAC,
#          CloudFront distribution, and bucket policy, prints the final
#          `notes` CNAME to add at Hover.
set -euo pipefail

DOMAIN="notes.harrywaterman.com"
BUCKET="notes-harrywaterman-com"
REGION="us-east-1"   # ACM certs used by CloudFront must live in us-east-1
OAC_NAME="meeting-notes-oac"
# Managed cache policy "CachingDisabled": unpublish must kill links
# instantly, and personal-scale traffic doesn't need caching.
CACHE_POLICY_ID="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

command -v aws >/dev/null || { echo "error: aws CLI not found" >&2; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Bucket (private, no listing) -------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
    echo "created bucket $BUCKET"
fi
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 2. Site assets ------------------------------------------------------------
aws s3 cp "$SCRIPT_DIR/site/404.html" "s3://$BUCKET/404.html" \
    --content-type "text/html; charset=utf-8"
aws s3 cp "$SCRIPT_DIR/site/robots.txt" "s3://$BUCKET/robots.txt" \
    --content-type "text/plain"

# 3. Certificate ------------------------------------------------------------
CERT_ARN="$(aws acm list-certificates --region us-east-1 \
    --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" \
    --output text)"
if [ "$CERT_ARN" = "None" ] || [ -z "$CERT_ARN" ]; then
    CERT_ARN="$(aws acm request-certificate --domain-name "$DOMAIN" \
        --validation-method DNS --region us-east-1 \
        --query CertificateArn --output text)"
    echo "requested certificate $CERT_ARN"
    sleep 10   # give ACM time to generate the validation record
fi
CERT_STATUS="$(aws acm describe-certificate --certificate-arn "$CERT_ARN" \
    --region us-east-1 --query Certificate.Status --output text)"
if [ "$CERT_STATUS" != "ISSUED" ]; then
    echo ""
    echo "== Certificate is $CERT_STATUS. Add this CNAME at Hover to validate =="
    aws acm describe-certificate --certificate-arn "$CERT_ARN" --region us-east-1 \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Name,Type,Value]' \
        --output text
    echo ""
    echo "Then re-run this script (validation usually takes 5-30 minutes)."
    exit 0
fi
echo "certificate ISSUED"

# 4. Origin Access Control --------------------------------------------------
OAC_ID="$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id | [0]" \
    --output text)"
if [ "$OAC_ID" = "None" ] || [ -z "$OAC_ID" ]; then
    OAC_ID="$(aws cloudfront create-origin-access-control \
        --origin-access-control-config \
        "Name=$OAC_NAME,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
        --query OriginAccessControl.Id --output text)"
    echo "created origin access control $OAC_ID"
fi

# 5. Distribution -----------------------------------------------------------
DIST_ID="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Aliases.Items && contains(Aliases.Items, '$DOMAIN')].Id | [0]" \
    --output text)"
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
    DIST_CONFIG="$(mktemp)"
    cat > "$DIST_CONFIG" <<EOF
{
  "CallerReference": "meeting-notes-$(date +%s)",
  "Comment": "meeting-notes unlisted note pages",
  "Enabled": true,
  "Aliases": {"Quantity": 1, "Items": ["$DOMAIN"]},
  "Origins": {"Quantity": 1, "Items": [{
      "Id": "s3-notes",
      "DomainName": "$BUCKET.s3.$REGION.amazonaws.com",
      "OriginAccessControlId": "$OAC_ID",
      "S3OriginConfig": {"OriginAccessIdentity": ""}
  }]},
  "DefaultCacheBehavior": {
      "TargetOriginId": "s3-notes",
      "ViewerProtocolPolicy": "redirect-to-https",
      "CachePolicyId": "$CACHE_POLICY_ID",
      "Compress": true
  },
  "CustomErrorResponses": {"Quantity": 2, "Items": [
      {"ErrorCode": 403, "ResponsePagePath": "/404.html",
       "ResponseCode": "404", "ErrorCachingMinTTL": 60},
      {"ErrorCode": 404, "ResponsePagePath": "/404.html",
       "ResponseCode": "404", "ErrorCachingMinTTL": 60}
  ]},
  "ViewerCertificate": {
      "ACMCertificateArn": "$CERT_ARN",
      "SSLSupportMethod": "sni-only",
      "MinimumProtocolVersion": "TLSv1.2_2021"
  }
}
EOF
    DIST_OUT="$(aws cloudfront create-distribution \
        --distribution-config "file://$DIST_CONFIG")"
    rm -f "$DIST_CONFIG"
    DIST_ID="$(echo "$DIST_OUT" | python3 -c \
        'import json,sys; print(json.load(sys.stdin)["Distribution"]["Id"])')"
    echo "created distribution $DIST_ID"
fi
DIST_DOMAIN="$(aws cloudfront get-distribution --id "$DIST_ID" \
    --query Distribution.DomainName --output text)"

# 6. Bucket policy: only this distribution may read -------------------------
POLICY="$(mktemp)"
cat > "$POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "cloudfront.amazonaws.com"},
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::$BUCKET/*",
    "Condition": {"StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::$ACCOUNT_ID:distribution/$DIST_ID"
    }}
  }]
}
EOF
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$POLICY"
rm -f "$POLICY"

echo ""
echo "== Done. Add this CNAME at Hover =="
echo "  notes  CNAME  $DIST_DOMAIN"
echo ""
echo "Then set in ~/.config/meeting-notes/config.yaml:"
echo "  upload_bucket: \"$BUCKET\""
echo "  upload_region: \"$REGION\""
echo "  upload_base_url: \"https://$DOMAIN\""
```

- [ ] **Step 3: Syntax-check and mark executable**

```bash
bash -n cloud/setup.sh
chmod +x cloud/setup.sh
```

Expected: `bash -n` exits 0 silently. Do **not** run the script for real in this task — AWS provisioning is a manual step Harry runs himself (spec: "exercised manually, not in CI").

- [ ] **Step 4: Commit**

```bash
git add cloud/setup.sh cloud/site/404.html cloud/site/robots.txt
git commit -m "feat: cloud/setup.sh provisions S3+CloudFront for notes.harrywaterman.com"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (add a "Publishing notes to the web" section near the existing feature/config docs)
- Modify: `CLAUDE.md` (short subsection so future sessions know the component exists)

**Interfaces:** none — docs only.

- [ ] **Step 1: README section**

Add (adjust placement to fit the README's existing section order, after the Obsidian/export material if present):

```markdown
## Publishing notes to the web (optional)

Press `u` on a note to publish its summary as an **unlisted** page at
`https://notes.harrywaterman.com/n/<random-slug>` and copy the link;
press `U` to unpublish (the link dies immediately). Unlisted means anyone
with the link can view it, but nothing lists or indexes the notes.

One-time setup:

1. `pip install 'meeting-notes[upload]'` (boto3 + markdown)
2. Run `cloud/setup.sh` (needs the AWS CLI with credentials). It prints
   two DNS records to add at your registrar: an ACM validation CNAME,
   then — after re-running once the cert validates — the final
   `notes → <distribution>.cloudfront.net` CNAME.
3. Set `upload_bucket` (and optionally `upload_region`,
   `upload_base_url`) in `~/.config/meeting-notes/config.yaml`.

Only the summary note is published — never the transcript or audio. The
share link is recorded as `share_url:` in the note's frontmatter.
```

- [ ] **Step 2: CLAUDE.md subsection**

Add under the Architecture section, after the `NoteMaker` bullet:

```markdown
4. **`uploader.py` (`NoteUploader`)** — optional unlisted web publishing
   (TUI keys `u`/`U`). Renders the summary note to self-contained HTML and
   PUTs it to S3 (served via CloudFront at `upload_base_url`); the link is
   recorded as `share_url:` in the note's frontmatter (single source of
   truth). Ordering guarantees: frontmatter written only after a successful
   PUT; on unpublish, S3 delete first, then frontmatter removal ("NoSuchKey"
   = success). boto3/markdown are lazy imports (`[upload]` extra); tests
   fake boto3 via `sys.modules`. Infra lives in `cloud/setup.sh`.
```

- [ ] **Step 3: Final verification and commit**

```bash
venv/bin/ruff check meeting_notes/ tests/
venv/bin/python -m pytest
git add README.md CLAUDE.md
git commit -m "docs: publishing notes to the web"
```

---

## Manual follow-up for Harry (not part of the code plan)

1. Run `cloud/setup.sh` (twice, with the ACM validation CNAME added at Hover in between).
2. Add the final `notes → <distribution>.cloudfront.net` CNAME at Hover.
3. Set `upload_bucket: "notes-harrywaterman-com"` in `~/.config/meeting-notes/config.yaml`.
4. `venv/bin/pip install boto3 markdown` (if not already done during Task 3).
5. Publish a real note with `u`, open the link in a browser, then `U` and confirm the link 404s.
