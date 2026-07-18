# Obsidian Summary Export

**Date:** 2026-07-16
**Status:** Approved (user directed: "wire summaries into an obsidian folder", standing autonomy preference)

## Goal

After each meeting, the generated summary note (`notes/<ts>-<title>.md`) also
lands in the user's Obsidian vault so meetings show up alongside their other
notes. Summaries only — transcripts (.txt) and recordings stay out of the
vault.

## Design

**Optional `obsidian_dir` config + best-effort copy on note save.**

- `Config.obsidian_dir: str = ""` — empty means the feature is off. No
  validation beyond what `Path.expanduser()` gives us; a bad path must never
  block note creation.
- `NoteMaker.__init__` gains `obsidian_dir: str = ""`. Unlike
  `output_dir`/`transcripts_dir`, the directory is NOT created at init
  (feature may be off; vault may be on a not-yet-mounted drive) — it is
  created on demand at copy time.
- In the note-save path, immediately after the note file is written: if
  `obsidian_dir` is set, `mkdir -p` it and write a byte-identical copy of the
  note under the same filename. Any exception is caught and logged
  (`logger.error`), and the save returns success — Obsidian export is
  strictly best-effort.
- `app.py` passes `obsidian_dir=self.config.obsidian_dir` at both `NoteMaker`
  construction sites (startup and settings-close reinit).
- Machine-local: set `obsidian_dir: /home/harry/Documents/Obsidian Vault/meetings`
  in `~/.config/meeting-notes/config.yaml`; create the folder.

Existing note frontmatter (title/date/tags) is already Obsidian-friendly and
is left unchanged. The `transcript_file` frontmatter field dangles inside the
vault copy — accepted; the canonical note in `notes/` still resolves it.

## Rejected alternatives

- **Point `notes_dir` into the vault** — zero code, but moves the app's
  canonical store into the vault and drags transcript references along.
- **Symlink `notes/` into the vault** — all-or-nothing, brittle with
  Obsidian indexing/sync, exposes non-summary files.

## Testing

`tests/test_obsidian_export.py` (fake-free, tmp_path, `ai_provider="none"`):

- obsidian_dir set → vault copy exists, same filename, identical content
- obsidian_dir empty (default) → nothing written outside notes/
- obsidian_dir uncreatable (file in the way) → note still saved, no raise
- vault dir auto-created when missing

## Out of scope

- Settings-screen UI for the new key (config.yaml edit is fine here)
- Wikilinks/backlinks, per-person note linking, transcript export
- Retroactive export of existing notes (a one-off copy can be done by hand)
