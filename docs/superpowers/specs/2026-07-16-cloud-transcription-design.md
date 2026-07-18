# Cloud Transcription (OpenAI, diarized)

**Date:** 2026-07-16
**Status:** Approved (user directed: "wire up cloud transcription with openai";
design presented in-conversation and accepted)

## Goal

Optional cloud transcription via OpenAI `gpt-4o-transcribe-diarize`:
~seconds instead of ~20 minutes per meeting on this CPU, with per-speaker
labels included. Local faster-whisper stays the default and the automatic
fallback — the privacy-first local pipeline remains intact unless the user
opts in.

## API contract (verified 2026-07-16, developers.openai.com)

- `POST /v1/audio/transcriptions`, model `gpt-4o-transcribe-diarize`
- `response_format="diarized_json"` → `segments: [{speaker, text, start, end}]`
- `chunking_strategy="auto"` (required for audio > 30s)
- 25 MB file cap; formats: mp3, mp4, mpeg, mpga, m4a, wav, webm (no ogg/opus
  container → compress to Opus **in WebM**)

## Design

### transcriber.py

- `TranscriptSegment` gains `speaker: Optional[str] = None` (default keeps
  all existing constructors/tests valid).
- `format_transcript_with_timestamps`: segments with a speaker render as
  `**[MM:SS] {speaker}:** text`; without, exactly today's format.
- New `OpenAITranscriber` with the same public surface as
  `WhisperTranscriber` (`load_model()` no-op, `transcribe(path,
  progress_callback=None) -> TranscriptResult`, `format_transcript_with_timestamps`):
  - `__init__(api_key, fallback)` — `fallback` is a constructed
    `WhisperTranscriber`.
  - `transcribe()`: compress with ffmpeg to mono 16 kbps Opus/WebM in a
    tempdir (a 4-hour meeting ≈ 22 MB, inside the cap); if the compressed
    file still exceeds 25 MB, or ffmpeg/the API/response parsing fails for
    ANY reason, log a warning and return `self.fallback.transcribe(...)`.
    Missing file raises FileNotFoundError (same as local; no fallback —
    nothing to transcribe).
  - Maps `diarized_json` segments (speaker/text/start/end) into
    `TranscriptSegment`s; text = joined stripped segment texts; duration =
    last segment end; language = `getattr(resp, "language", "unknown")`.
  - `openai` imported lazily inside `transcribe()`.
- Factory `create_transcriber(config) -> WhisperTranscriber | OpenAITranscriber`:
  `transcription_provider == "openai"` AND an OpenAI key available (config
  or `OPENAI_API_KEY` env) → `OpenAITranscriber` with local fallback;
  anything else → `WhisperTranscriber` exactly as today.

### config.py

- `transcription_provider: str = "local"` — `"local"` | `"openai"`.
- Validation rejects other values.

### app.py

- Both transcriber construction sites become `create_transcriber(self.config)`.

## Testing

`tests/test_cloud_transcriber.py` (fake OpenAI client injected on the
instance; no network):

- diarized segments map to TranscriptSegments with speakers; text join;
  duration from last segment
- API error → falls back to the injected fake local transcriber (result
  passthrough)
- oversized compressed file → fallback (simulate via monkeypatched size check)
- factory: default config → WhisperTranscriber; provider=openai + key →
  OpenAITranscriber; provider=openai w/o key → WhisperTranscriber
- formatter renders speaker labels; speakerless segments render as before
- config validation: local/openai ok, junk rejected
- compression helper: real ffmpeg on a generated 1s wav → .webm exists
  (skipped if ffmpeg missing)

CI: add the new test file to the explicit pytest list (fakes only, no heavy
deps beyond `openai`, already installed in CI).

## Addendum 2026-07-16 (same day): known-speaker reference

User directed. New config keys `speaker_name` / `speaker_reference` (both
default `""` = off). When both are set and the file exists,
`OpenAITranscriber.transcribe` adds `known_speaker_names=[name]` and
`known_speaker_references=["data:audio/wav;base64,..."]` (clip must be
2–10s; SDK 2.45 supports the params natively). Segments matching the
reference come back labeled with the name instead of "A". Missing/unreadable
clip → log a warning and transcribe without references (never fail).
Machine-local: 8.8s of Harry's mic leg extracted to
`~/.config/meeting-notes/voice-harry.wav`, config points at it.

## Out of scope

- Auto-labeling the remaining letter speaker with the other party's name
  from the meeting title (heuristic; not v1)
- Settings-screen UI for the provider toggle (config.yaml is fine)
- Streaming transcription; chunking files > 25 MB post-compression
- AssemblyAI/Deepgram backends
