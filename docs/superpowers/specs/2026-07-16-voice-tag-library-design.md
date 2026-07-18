# Voice Tag Library

**Date:** 2026-07-16
**Status:** Approved (user: "yea" to the presented v1 design)

## Goal

A library of known-speaker voice clips that makes every cloud transcript
come back with real names, plus a command that grows the library from
already-labeled transcript lines — no manual audio editing.

## Design

### Library

- `~/.config/meeting-notes/voices/` — one audio clip per person; the
  filename stem is the speaker name (`harry.wav` → "harry"... names are
  used as-is, so files should be capitalized as desired, e.g. `Harry.wav`).
- Config: `voices_dir: str = "~/.config/meeting-notes/voices"`. Replaces
  the hours-old `speaker_name`/`speaker_reference` keys (removed; stale
  yaml keys are filtered by `AppConfig.from_dict`). Machine-local:
  `voice-harry.wav` moves to `voices/Harry.wav`.
- `OpenAITranscriber` takes `voices_dir`; at transcribe time it scans for
  supported clips (`.wav .mp3 .m4a .webm` — must be 2–10s per API), takes
  the **4 most recently modified** (API cap), and sends
  `known_speaker_names` + `known_speaker_references` data URLs (mime by
  extension). Missing/empty dir → no kwargs; unreadable clip → skipped
  with a warning. Never fails a transcription.
- MRU ordering means tagging someone bumps them into the active 4 — the
  people you actually meet stay resolvable.

### Tagging command

`python -m meeting_notes.voice_tag <transcript> <label> <name> [--recording PATH]`

- Parses `**[MM:SS] A:** text` lines (also `H:MM:SS`; unlabeled lines are
  ignored). Segment duration is estimated as the gap to the next line's
  timestamp, capped at 10s.
- Picks the label's best utterance: longest estimated duration (min 2s),
  ties broken by word count; cuts `[start, start+min(gap,10s)]` from the
  recording with ffmpeg (mono 16 kHz wav) into `voices/<name>.wav`.
- Recording path from `--recording`, or the transcript's `Recording:`
  header when present; a clear error if neither exists.
- Prints the chosen line's timestamp + text so the user can sanity-check
  the clip is really that person.

## Testing

- Parser: labeled/unlabeled/H:MM:SS lines; gap-based durations.
- Best-utterance picker: longest-gap wins, <2s candidates skipped.
- Library scan: MRU top-4, name = stem, correct mime in data URL,
  missing dir → {}.
- Transcriber sends multiple names/refs from a populated tmp library.
- Factory passes `voices_dir` from config.
- tag-voice end-to-end on a tmp transcript + generated wav (skipif no
  ffmpeg).

## Out of scope

- TUI integration (notes-browser action) — later.
- Auto-suggesting "who is A?" after meetings.
- Per-meeting speaker-set selection beyond MRU.
