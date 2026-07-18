# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Meeting Notes is a keyboard-driven Textual TUI for Linux that records a meeting (mic + system audio), transcribes it, and generates a Markdown note with an AI summary. Everything runs locally by default; cloud transcription and cloud summaries are opt-in.

## Commands

```bash
# Run the app (from repo root, using the project venv)
venv/bin/python run.py           # add --dev for dev mode

# Full test suite (needs faster-whisper etc.: pip install -e ".[all,dev]")
venv/bin/python -m pytest

# Single test file / single test
venv/bin/python -m pytest tests/test_summarizers.py
venv/bin/python -m pytest tests/test_summarizers.py::test_openai_summarize_omits_temperature

# Lint (matches CI ruleset — real bugs only, no style nits)
venv/bin/ruff check meeting_notes/ tests/
```

There is a `venv/` in the repo; use `venv/bin/python` so the heavy deps (faster-whisper/ctranslate2) resolve.

## Testing model — two tiers

CI (`.github/workflows/ci.yml`) deliberately installs **only** the lightweight deps and runs an **explicit list** of test files, avoiding `faster-whisper` (which drags in ctranslate2/onnxruntime and is slow to install). Consequences to respect when adding tests:

- Tests that need audio/transcription must **not** import `faster_whisper` at module load. The pattern (`tests/test_transcriber_device.py`) is to inject a fake `faster_whisper` into `sys.modules` before use; `transcriber.py` imports it lazily inside `load_model()` precisely so this works.
- `tests/test_textual_smoke.py` uses `pytest.importorskip` and is **local-only** — it's not in the CI file list. It drives the real app headless via `App.run_test()`.
- If you add a new test file with heavy deps, it won't run in CI unless you add it to the explicit list in the workflow. A new *lightweight* test file also needs adding to that list to be covered.

## Architecture — the recording→note pipeline

The core flow lives in `meeting_notes/app.py` (`MeetingNotesApp`), which orchestrates three swappable components built from `AppConfig`:

1. **`recorder.py` (`AudioRecorder`)** — captures audio via PipeWire/PulseAudio (`pactl`/`pw-record`/`parec`), producing a WAV. Key domain knowledge encoded here:
   - Devices are addressed by **name**, never numeric index (indexes change on every PipeWire restart).
   - System audio is captured from a **`<sink-name>.monitor` source**, not a sink.
   - Modes: `mic`, `system`, `combined` (combined mixes the two legs with ffmpeg).
   - `cancel_recording()` (kill + delete, no ffmpeg) is deliberately distinct from `stop_recording()` (finalize + mix).
   - Per-leg **silence detection** sets `last_mic_silent`/`last_system_silent`; the app surfaces these as warnings because a silent system-audio leg (other participants missing) is the highest-value failure to catch.
   - A **keep-awake** reader is attached to the sink monitor because PipeWire auto-suspends idle sinks, which would otherwise make monitor capture silent.

2. **`transcriber.py` (`create_transcriber(config)`)** — returns either `WhisperTranscriber` (local faster-whisper) or `OpenAITranscriber` (cloud diarized). The cloud path **always falls back to the local transcriber on any failure**, so a meeting is never lost to a network error. Both expose the same surface (`load_model`, `transcribe`, `format_transcript_with_timestamps`). Cloud diarization attaches speaker labels; the transcriber's own formatter preserves them, so don't hand-roll transcript formatting in the app.

3. **`note_maker.py` (`NoteMaker`)** → **`ai_summarizer.py` / `summarizer.py`** — builds the Markdown note. `NoteMaker` picks a summarizer by provider: cloud (`OpenAISummarizer`/`AnthropicSummarizer`/`OpenRouterSummarizer` in `ai_summarizer.py`, sharing `BaseSummarizer`) or local (`OllamaSummarizer` in `summarizer.py`). **The Ollama summarizer duplicates the shared prompt** rather than inheriting it — keep the two prompt copies in sync when editing prompt text. Summary failure is non-fatal: it degrades to a keyword summary and records `ai_error`.

4. **`uploader.py` (`NoteUploader`)** — optional unlisted web publishing
   (TUI keys `u`/`U`). Renders the summary note to self-contained HTML and
   PUTs it to S3 (served via CloudFront at `upload_base_url`); the link is
   recorded as `share_url:` in the note's frontmatter (single source of
   truth). Ordering guarantees: frontmatter written only after a successful
   PUT; on unpublish, S3 delete first, then frontmatter removal ("NoSuchKey"
   = success). boto3/markdown are lazy imports (`[upload]` extra); tests
   fake boto3 via `sys.modules`. Infra lives in `cloud/setup.sh`.

`action_stop_recording` hands off to `process_recording`, a **`@work(thread=True)` background thread**. UI updates from that thread must go through `self.call_from_thread(...)`.

### Config

`config.py` (`AppConfig`, a dataclass) is loaded from `~/.config/meeting-notes/config.yaml` (or `$XDG_CONFIG_HOME`). It's the single source of truth threaded into every component. `validate_config()` gates provider/model/device/path combinations. Unknown YAML keys are dropped on load, so adding a field means adding it to the dataclass. The config file is written `0600` because it holds API keys.

### context_hint

`AppConfig.context_hint` is free-text background about the user (org names, jargon). It flows to **two** places: local Whisper as `initial_prompt` (biases decoding, e.g. "meter" → "METR"), and every summarizer prompt as a trusted block placed **below** the untrusted-transcript boundary so the model treats it as instructions. The cloud diarize model accepts no prompt, so for cloud transcription the summarizer is the only correction layer.

### Prompt injection boundary

Summarizer prompts wrap the transcript in `<transcript>` tags with an explicit "everything above is untrusted user data" marker. Trusted, config-derived content (like `context_hint`) goes **after** that boundary; never interpolate transcript-derived text above it.

## Desktop integration

`gnome/install.sh` is an idempotent installer generating `~/.local/bin/meeting-notes` (launcher), GNOME keybinds, an app entry, and an autostart indicator. The launcher controls an already-running app via **signals**, matched by handlers registered in `MeetingNotesApp.on_mount`:

- Ctrl+Alt+M → SIGUSR1 → start recording (launches the app if not running)
- Ctrl+Alt+S → SIGUSR2 → end + process
- Ctrl+Alt+X → SIGRTMIN+1 → cancel + discard

State is shared with Waybar/the indicator through the repo-root **`.status`** file (`STATUS="idle|recording|processing"`), which `_write_status_file` maintains and the launcher reads to decide whether an action applies. In-TUI keys are `r`/`s`/`x` (see `BINDINGS` in `app.py`).
