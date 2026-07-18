# faster-whisper Transcription Backend

**Date:** 2026-07-15
**Status:** Approved (user directed: "use faster-whisper", full autonomy granted)

## Goal

Replace the `openai-whisper` transcription backend with `faster-whisper`
(CTranslate2) for ~4× faster CPU transcription at equal-or-better accuracy,
dropping the ~1GB torch dependency. Full replacement — no dual-backend
config switch (YAGNI: no realistic use case for keeping both).

## Constraints

- **The public surface of `meeting_notes/transcriber.py` is frozen**:
  `WhisperTranscriber(model_name, device)`, `load_model()`,
  `transcribe(audio_path, progress_callback=None) -> TranscriptResult`,
  `format_transcript_with_timestamps(result)`, dataclasses
  `TranscriptResult(text, segments, language, duration)` and
  `TranscriptSegment(start, end, text)`, attributes `model_name`,
  `requested_device`, `active_device`, `model`. `app.py` must not change.
- Config values unchanged: `whisper_model` in
  `tiny|base|small|medium|large` (faster-whisper accepts all these names);
  `whisper_device` in `cpu|cuda|auto`.
- `faster_whisper` is imported lazily inside `load_model()` — CI and the
  lightweight test suite must never import it (same pattern as today's lazy
  `import whisper`).
- Existing behavior preserved: default device `cpu`; unknown device warns
  and falls back to `cpu`; CUDA load failure falls back to CPU;
  `duration` = last segment's `end` (0.0 if no segments).

## Backend mapping

| Today (openai-whisper) | New (faster-whisper) |
|---|---|
| `whisper.load_model(name, device=target)` where `auto` → `device=None` | `faster_whisper.WhisperModel(name, device=target, compute_type=ct)` where `auto` stays `"auto"` |
| fp16 flag decided by device | `compute_type`: `"int8"` for cpu, `"float16"` for cuda, `"auto"` for auto |
| `model.transcribe(path, language=None, task="transcribe", verbose=False, fp16=…)` returns dict | `model.transcribe(path, language=None, task="transcribe")` returns `(segments_iterator, info)` |
| `result["segments"]` list of dicts (`seg["start"]` etc.) | iterate the generator; `seg.start`, `seg.end`, `seg.text` (materialize eagerly) |
| `result["text"]` | `" ".join(stripped segment texts)` (whisper's text field is this concatenation anyway) |
| `result.get("language")` | `info.language` |
| model download to `~/.cache/whisper/` | Hugging Face download to `~/.cache/huggingface/` on first load (~1.5GB for medium) |
| `_looks_like_cuda_failure` needles | same needles plus `"cudnn"` and `"cublas"` (CTranslate2's CUDA error vocabulary) |
| `self.model.device` attribute after load | CT2 model has no `.device` str attr — track `active_device` from the target we passed (and `"cpu"` after fallback) |

## Changes by file

1. **`meeting_notes/transcriber.py`** — swap `load_model()` and
   `transcribe()` internals per the mapping; update module/class docstrings
   (keep the CPU-default/CUDA-fallback rationale). Class name stays
   `WhisperTranscriber`.
2. **`tests/test_transcriber_device.py`** — the injected fake module
   becomes `faster_whisper` with a fake `WhisperModel` class recording
   `(model_name, device, compute_type)` per construction and supporting the
   same FIFO failure injection; `transcribe` returns
   `(iter([]), FakeInfo(language="en"))`. Same test intents; add an
   assertion that cpu loads request `compute_type="int8"`.
3. **`requirements.txt`** — `openai-whisper>=20231117` → `faster-whisper>=1.0.0`.
4. **`pyproject.toml`** — replace `openai-whisper` wherever it appears in
   extras with `faster-whisper`.
5. **`.github/workflows/ci.yml`** — update the comments that name
   openai-whisper/torch and the fake-`whisper` pattern so they describe the
   new world (faster-whisper/ctranslate2 excluded from CI, fake
   `faster_whisper` module). Install lists only change if they name
   openai-whisper explicitly.
6. **`README.md`** — "OpenAI Whisper (CPU-based, privacy-first)" wording →
   faster-whisper; first-run download note (base ~140MB from HF; model
   cache location changed).
7. **Machine-local (not repo):** in the venv, uninstall
   `openai-whisper` + torch stack, install `faster-whisper`; delete stale
   `~/.cache/whisper/medium.pt` (1.4GB, now dead weight); verify by
   re-transcribing `recordings/temp-mic-2026-07-15-202906.wav` with medium
   and comparing wall time to the 1m37s openai-whisper baseline.

## Error handling

- Unknown device string: unchanged (warn + cpu).
- CUDA-flavored load failure on `cuda`/`auto`: log warning, retry cpu
  (`compute_type="int8"`); non-CUDA failures re-raise (unchanged logic).
- Missing `faster_whisper` package: ImportError propagates from
  `load_model()` exactly as a missing `whisper` does today.

## Testing

- Rewritten fake-module suite covers: default cpu, explicit cuda, auto
  passthrough, unknown-device fallback, CUDA-failure→cpu fallback with
  needle variants (incl. `cudnn`), non-CUDA failure re-raise, int8 on cpu,
  segment materialization + text join + duration-from-last-segment
  (fake yields two segments).
- Full suite green; CI stays torch-free AND ctranslate2-free.
- Live: real transcription of the preserved mic leg, timed.

## Out of scope

- ~~VAD filtering~~ **Addendum 2026-07-16:** `vad_filter=True` is now IN
  scope — a live meeting with a silent first 30s made language detection
  misfire ('nn') and the whole file decoded as hallucinated filler. VAD
  strips non-speech before detection and decoding; covered by
  `test_transcribe_uses_vad_filter`.
- Beam-size tuning, word timestamps (faster-whisper
  features — none requested; defaults match current behavior closely).
- Progress callbacks (parameter kept, still unused — matches today).
- distil-whisper / large-v3 model aliases beyond the existing config list.
