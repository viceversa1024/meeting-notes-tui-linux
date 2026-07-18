# faster-whisper Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the openai-whisper transcription backend with faster-whisper (CTranslate2) for ~4× faster CPU transcription, preserving `transcriber.py`'s public surface exactly.

**Architecture:** `WhisperTranscriber` keeps its class name, constructor, attributes, and `transcribe() -> TranscriptResult` contract; only `load_model()`/`transcribe()` internals change. `faster_whisper` is imported lazily inside `load_model()` so CI and the lightweight suite never load it — tests inject a fake `faster_whisper` module into `sys.modules`, replacing today's fake `whisper`.

**Tech Stack:** faster-whisper >= 1.0.0 (CTranslate2), pytest with fake-module injection, repo venv.

## Global Constraints

- Public surface of `meeting_notes/transcriber.py` is FROZEN: `WhisperTranscriber(model_name: str = "base", device: str = "cpu")`, `.load_model()`, `.transcribe(audio_path, progress_callback=None) -> TranscriptResult`, `.format_transcript_with_timestamps(result)`, `TranscriptResult(text, segments, language, duration)`, `TranscriptSegment(start, end, text)`, attributes `model_name`, `requested_device`, `active_device`, `model`. `meeting_notes/app.py` must NOT change.
- `import` of `faster_whisper` appears ONLY inside `load_model()` (lazy).
- Compute types: cpu → `"int8"`, cuda → `"float16"`, auto → `"auto"`. CUDA-failure CPU fallback always uses `("cpu", "int8")`.
- `duration` = last segment's `end`, `0.0` when no segments. `text` = stripped segment texts joined with single spaces.
- Config value sets unchanged: models `tiny|base|small|medium|large`, devices `cpu|cuda|auto` (`auto` now passes through as the string `"auto"`, not `None`).
- Tests run with the repo venv: `venv/bin/python -m pytest`. CI must stay free of torch AND ctranslate2.
- Never `git add` anything under `docs/` (gitignored).
- Work on branch `faster-whisper` (created from `main` after the GNOME branch merges).

---

### Task 1: Swap transcriber internals to faster-whisper (TDD)

**Files:**
- Modify: `meeting_notes/transcriber.py` (module docstring, `_resolve_device`, `load_model`, `transcribe`; add `_compute_type_for`; extend `_looks_like_cuda_failure` needles)
- Modify: `tests/test_transcriber_device.py` (full rewrite of the fake-module fixture and tests)

**Interfaces:**
- Consumes: current `transcriber.py` (openai-whisper internals).
- Produces: same public surface (see Global Constraints) backed by `faster_whisper.WhisperModel`; module-level helper `_compute_type_for(device: str) -> str`.

- [ ] **Step 1: Rewrite the test file (this is the failing-test step)**

Replace the entire contents of `tests/test_transcriber_device.py` with:

```python
"""Tests for WhisperTranscriber device handling and CUDA fallback.

We don't import the real ``faster_whisper`` (and hence ctranslate2) — we
install a fake ``faster_whisper`` module into ``sys.modules`` before
WhisperTranscriber tries to load it. This keeps CI fast and matches the
repo pattern of keeping heavyweight ML wheels out of the test suite.
"""

from __future__ import annotations

import sys
import types

import pytest

from meeting_notes.transcriber import WhisperTranscriber


class FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    def __init__(self, language="en"):
        self.language = language


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """Install a controllable fake `faster_whisper` module for the test."""
    fake_mod = types.ModuleType("faster_whisper")
    fake_mod._failures = []  # list of (device_or_None, exception), FIFO
    fake_mod._loads = []     # appended (model_name, device, compute_type)
    fake_mod._segments = []  # segments the next transcribe() yields

    class FakeWhisperModel:
        def __init__(self, model_name, device="auto", compute_type="default"):
            fake_mod._loads.append((model_name, device, compute_type))
            if fake_mod._failures:
                should_fail_for, exc = fake_mod._failures[0]
                if should_fail_for is None or should_fail_for == device:
                    fake_mod._failures.pop(0)
                    raise exc
            self.model_name = model_name
            self.device = device
            self.compute_type = compute_type
            self.transcribe_calls = []

        def transcribe(self, path, **kwargs):
            self.transcribe_calls.append((path, kwargs))
            return iter(fake_mod._segments), FakeInfo()

    fake_mod.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    yield fake_mod


# ----- device / compute_type selection -----

def test_default_device_is_cpu_int8(fake_faster_whisper):
    t = WhisperTranscriber("base")
    t.load_model()
    assert fake_faster_whisper._loads == [("base", "cpu", "int8")]
    assert t.active_device == "cpu"


def test_explicit_cuda_uses_float16(fake_faster_whisper):
    t = WhisperTranscriber("base", device="cuda")
    t.load_model()
    assert fake_faster_whisper._loads == [("base", "cuda", "float16")]
    assert t.active_device == "cuda"


def test_auto_passes_through(fake_faster_whisper):
    t = WhisperTranscriber("base", device="auto")
    t.load_model()
    assert fake_faster_whisper._loads == [("base", "auto", "auto")]


def test_unknown_device_falls_back_to_cpu(fake_faster_whisper):
    t = WhisperTranscriber("base", device="tpu")
    assert t.requested_device == "cpu"
    t.load_model()
    assert fake_faster_whisper._loads == [("base", "cpu", "int8")]


def test_load_model_is_lazy_and_cached(fake_faster_whisper):
    t = WhisperTranscriber("base")
    assert fake_faster_whisper._loads == []
    t.load_model()
    t.load_model()
    assert len(fake_faster_whisper._loads) == 1


# ----- CUDA failure fallback -----

def test_cuda_failure_falls_back_to_cpu(fake_faster_whisper):
    fake_faster_whisper._failures.append(
        ("cuda", RuntimeError("CUDA error: no kernel image is available"))
    )
    t = WhisperTranscriber("base", device="cuda")
    t.load_model()
    assert fake_faster_whisper._loads == [
        ("base", "cuda", "float16"),
        ("base", "cpu", "int8"),
    ]
    assert t.active_device == "cpu"


def test_cudnn_failure_falls_back_to_cpu(fake_faster_whisper):
    fake_faster_whisper._failures.append(
        ("cuda", RuntimeError("Unable to load libcudnn_ops.so.9: cudnn missing"))
    )
    t = WhisperTranscriber("base", device="cuda")
    t.load_model()
    assert t.active_device == "cpu"


def test_non_cuda_failure_reraises(fake_faster_whisper):
    fake_faster_whisper._failures.append(
        ("cuda", RuntimeError("model archive is corrupt"))
    )
    t = WhisperTranscriber("base", device="cuda")
    with pytest.raises(RuntimeError, match="corrupt"):
        t.load_model()


def test_cpu_failure_reraises_without_retry(fake_faster_whisper):
    fake_faster_whisper._failures.append(
        (None, RuntimeError("CUDA error: even the needle does not save cpu"))
    )
    t = WhisperTranscriber("base", device="cpu")
    with pytest.raises(RuntimeError):
        t.load_model()
    assert len(fake_faster_whisper._loads) == 1


# ----- transcribe() result shaping -----

def _tone_file(tmp_path):
    p = tmp_path / "audio.wav"
    p.write_bytes(b"RIFFfakewav")
    return p


def test_transcribe_materializes_segments(fake_faster_whisper, tmp_path):
    fake_faster_whisper._segments = [
        FakeSegment(0.0, 1.5, " Hello "),
        FakeSegment(1.5, 3.25, " world "),
    ]
    t = WhisperTranscriber("base")
    result = t.transcribe(str(_tone_file(tmp_path)))
    assert [s.text for s in result.segments] == ["Hello", "world"]
    assert result.text == "Hello world"
    assert result.duration == 3.25
    assert result.language == "en"


def test_transcribe_empty_audio(fake_faster_whisper, tmp_path):
    fake_faster_whisper._segments = []
    t = WhisperTranscriber("base")
    result = t.transcribe(str(_tone_file(tmp_path)))
    assert result.segments == []
    assert result.text == ""
    assert result.duration == 0.0


def test_transcribe_missing_file_raises(fake_faster_whisper, tmp_path):
    t = WhisperTranscriber("base")
    with pytest.raises(FileNotFoundError):
        t.transcribe(str(tmp_path / "missing.wav"))
```

- [ ] **Step 2: Run the rewritten tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_transcriber_device.py -q`
Expected: FAIL — the current implementation imports `whisper` (not `faster_whisper`), so `load_model()` raises `ModuleNotFoundError: No module named 'whisper'`... unless openai-whisper is still installed in the venv, in which case tests fail on the `_loads` assertions instead (the fake `faster_whisper` is never touched). Either failure mode is the expected RED.

- [ ] **Step 3: Rewrite the transcriber internals**

In `meeting_notes/transcriber.py`:

3a. Replace the module docstring (lines 1-10) with:

```python
"""Transcription module using faster-whisper (CTranslate2).

faster-whisper runs Whisper models ~4x faster than openai-whisper on CPU
(int8 quantization) with equal-or-better accuracy, and without the ~1GB
torch dependency. We default to CPU to match the README's "CPU-based,
privacy-first" promise, allow opt-in CUDA via config, and transparently
fall back to CPU if the chosen device can't actually load the model
(broken CUDA installs, missing cuDNN/cuBLAS, etc.).
"""
```

3b. Extend `_looks_like_cuda_failure`'s `needles` tuple with CTranslate2's CUDA error vocabulary — replace the existing tuple with:

```python
    needles = (
        "no kernel image is available",
        "CUDA error",
        "CUDA driver",
        "Torch not compiled with CUDA",
        "cudaError",
        "device-side assert",
        "cudnn",
        "cublas",
    )
```

3c. Add a module-level helper directly below `_looks_like_cuda_failure`:

```python
def _compute_type_for(device: str) -> str:
    """CTranslate2 compute type per device: int8 is the CPU speed win."""
    if device == "cpu":
        return "int8"
    if device == "cuda":
        return "float16"
    return "auto"
```

3d. Replace `_resolve_device` with (faster-whisper takes `"auto"` directly; no more `None`):

```python
    def _resolve_device(self) -> str:
        """The device string to hand faster-whisper ("auto" passes through)."""
        return self.requested_device
```

3e. Replace `load_model` entirely with:

```python
    def load_model(self):
        """Load the faster-whisper model (lazy loading), with CUDA-failure fallback."""
        if self.model is not None:
            return

        # Import lazily so unit tests / non-transcription code paths don't
        # need the faster-whisper/ctranslate2 wheels installed.
        from faster_whisper import WhisperModel  # noqa: WPS433 (intentional local import)

        target = self._resolve_device()
        compute_type = _compute_type_for(target)
        try:
            logger.info(
                f"Loading faster-whisper {self.model_name} model "
                f"(device={target}, compute_type={compute_type})..."
            )
            self.model = WhisperModel(
                self.model_name, device=target, compute_type=compute_type
            )
            self.active_device = target
            logger.info(f"faster-whisper model loaded successfully on {target}")
            return
        except Exception as exc:  # noqa: BLE001 - handle anything ctranslate2 throws
            if target == "cpu" or not _looks_like_cuda_failure(exc):
                logger.error(f"faster-whisper model load failed: {exc}", exc_info=True)
                raise

            logger.warning(
                f"faster-whisper failed to load on {target} ({exc}). "
                "Falling back to CPU."
            )
            try:
                self.model = WhisperModel(
                    self.model_name, device="cpu", compute_type="int8"
                )
                self.active_device = "cpu"
                logger.info(
                    "faster-whisper model loaded successfully on cpu (after CUDA failure)"
                )
            except Exception as cpu_exc:
                logger.error(f"CPU fallback also failed: {cpu_exc}", exc_info=True)
                raise
```

3f. In `transcribe()`, replace everything from the `use_fp16 = ...` line through the `return TranscriptResult(...)` with:

```python
        raw_segments, info = self.model.transcribe(
            str(audio_file),
            language=None,
            task="transcribe",
        )

        # faster-whisper returns a lazy generator; materialize it so the
        # result is complete before we hand it back.
        segments = [
            TranscriptSegment(start=seg.start, end=seg.end, text=seg.text.strip())
            for seg in raw_segments
        ]

        duration = segments[-1].end if segments else 0.0
        text = " ".join(seg.text for seg in segments).strip()
        language = getattr(info, "language", "unknown") or "unknown"

        logger.info(
            f"Transcription complete: {len(segments)} segments, "
            f"{duration:.1f}s duration, language: {language}"
        )

        return TranscriptResult(
            text=text,
            segments=segments,
            language=language,
            duration=duration,
        )
```

Also update the class docstring `"""Transcribe audio files using Whisper."""` → `"""Transcribe audio files using faster-whisper."""` and the `__init__` docstring's device explanation sentence `"auto" lets Whisper pick (CUDA when available)` → `"auto" lets faster-whisper pick (CUDA when available)`.

- [ ] **Step 4: Run the rewritten tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_transcriber_device.py -q`
Expected: `13 passed`

- [ ] **Step 5: Full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all pass, no new warnings.

- [ ] **Step 6: Commit**

```bash
git add meeting_notes/transcriber.py tests/test_transcriber_device.py
git commit -m "transcriber: swap openai-whisper for faster-whisper (CTranslate2)"
```

---

### Task 2: Dependency metadata, CI comments, README wording

**Files:**
- Modify: `requirements.txt` (line 4)
- Modify: `pyproject.toml` (line 32)
- Modify: `.github/workflows/ci.yml` (two comment blocks)
- Modify: `README.md` (lines 3, 9, 65, 451 area)

**Interfaces:**
- Consumes: Task 1's swapped backend (docs must describe what the code now does).
- Produces: metadata consistent with the faster-whisper backend.

- [ ] **Step 1: requirements.txt**

Replace the line `openai-whisper>=20231117` with:

```
faster-whisper>=1.0.0
```

- [ ] **Step 2: pyproject.toml**

At line 32, replace `    "openai-whisper>=20231117",` with:

```
    "faster-whisper>=1.0.0",
```

- [ ] **Step 3: ci.yml comment blocks**

Replace the "Install lightweight test deps" comment lines:

```yaml
        # Deliberately avoiding the [all] extra: that pulls openai-whisper
        # which transitively installs torch (~1GB) and explodes CI time.
```

with:

```yaml
        # Deliberately avoiding the [all] extra: that pulls faster-whisper
        # and its ctranslate2 wheel, which explodes CI install time.
```

Replace the "Test (pytest, fast tests only)" comment lines:

```yaml
        # The new audio/transcriber tests deliberately avoid importing torch
        # (transcriber.py imports whisper lazily inside load_model, and the
        # tests inject a fake whisper module via sys.modules), so they're
        # safe to run on the lightweight CI image.
```

with:

```yaml
        # The audio/transcriber tests deliberately avoid importing
        # ctranslate2 (transcriber.py imports faster_whisper lazily inside
        # load_model, and the tests inject a fake faster_whisper module via
        # sys.modules), so they're safe on the lightweight CI image.
```

- [ ] **Step 4: README wording**

Line 3: replace `transcribe with Whisper` with `transcribe locally with faster-whisper`.
Line 9: replace `- **Local transcription** - OpenAI Whisper (CPU-based, privacy-first)` with:

```markdown
- **Local transcription** - faster-whisper (CTranslate2; CPU-based, privacy-first)
```

Line 65: replace the note `**Note:** The first time you run transcription, Whisper will download the `base` model (~140MB).` with:

```markdown
**Note:** The first time you run transcription, faster-whisper downloads the
model from Hugging Face into `~/.cache/huggingface/` (~140MB for `base`,
~1.5GB for `medium`).
```

Line 451: replace `# Lightweight tests (matches CI — no whisper/torch needed)` with `# Lightweight tests (matches CI — no faster-whisper/ctranslate2 needed)`.

- [ ] **Step 5: Verify nothing in the repo still names openai-whisper or torch**

Run: `grep -rn "openai-whisper\|import whisper\|fake whisper" --include="*.py" --include="*.txt" --include="*.toml" --include="*.yml" --include="*.md" . | grep -v venv | grep -v docs/ | grep -v "faster"`
Expected: no hits (transcripts/notes directories excluded from concern).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pyproject.toml .github/workflows/ci.yml README.md
git commit -m "deps/docs: openai-whisper -> faster-whisper everywhere"
```

---

### Task 3: Machine-local venv swap + live benchmark (controller runs inline)

**Files:** none in repo (environment + verification only)

**Interfaces:**
- Consumes: Tasks 1-2 committed.
- Produces: working local install, timed proof of the speedup.

- [ ] **Step 1: Swap the venv packages**

```bash
venv/bin/pip uninstall -y openai-whisper torch triton
venv/bin/pip install faster-whisper
```
Expected: uninstalls succeed (triton may be absent — fine); faster-whisper + ctranslate2 install.

- [ ] **Step 2: Delete the stale openai-whisper model cache**

```bash
rm -f ~/.cache/whisper/medium.pt ~/.cache/whisper/base.pt
```

- [ ] **Step 3: Timed live transcription of the real recording**

```bash
time venv/bin/python -c "
from meeting_notes.transcriber import WhisperTranscriber
t = WhisperTranscriber('medium', device='cpu')
r = t.transcribe('recordings/temp-mic-2026-07-15-202906.wav')
print(len(r.segments), 'segments;', r.duration, 's;', r.text[:120])
"
```
Expected: sensible English transcript of the walking conversation; wall time meaningfully under the 97s openai-whisper baseline for the same file (first run includes the ~1.5GB model download — time the transcription from the log timestamps or run twice).

- [ ] **Step 4: Full suite one more time in the swapped venv**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all pass (proves the real faster-whisper install doesn't break the fake-module injection).
