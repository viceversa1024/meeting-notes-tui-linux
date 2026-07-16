"""Tests for WhisperTranscriber device handling and CUDA fallback.

We don't import the real ``faster_whisper`` (and hence ctranslate2) — we
install a fake ``faster_whisper`` module into ``sys.modules`` before
WhisperTranscriber tries to load it. This keeps CI fast and matches the
existing pattern of avoiding heavy ML wheels in the lightweight test suite.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str


@dataclass
class FakeInfo:
    language: str = "en"
    duration: float = 0.0


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """Install a controllable fake `faster_whisper` module for the test."""
    fake_mod = types.ModuleType("faster_whisper")

    fake_mod._failures = []  # set by tests: list of (device, exception)
    fake_mod._loads = []     # appended (model_name, device, compute_type) per call
    fake_mod._segments = []  # segments returned by transcribe
    fake_mod._info = FakeInfo()

    class FakeWhisperModel:
        def __init__(self, model_name, device="auto", compute_type="default", **kwargs):
            fake_mod._loads.append((model_name, device, compute_type))
            if fake_mod._failures:
                should_fail_for, exc = fake_mod._failures[0]
                if should_fail_for is None or should_fail_for == device:
                    fake_mod._failures.pop(0)
                    raise exc
            self.device = device
            self.transcribe_calls = []

        def transcribe(self, path, **kwargs):
            self.transcribe_calls.append((path, kwargs))
            return iter(fake_mod._segments), fake_mod._info

    fake_mod.WhisperModel = FakeWhisperModel

    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    # Guarantee the old openai-whisper backend can't be silently used.
    monkeypatch.setitem(sys.modules, "whisper", None)
    yield fake_mod


def _transcriber_module():
    # transcriber imports faster_whisper lazily inside load_model.
    from meeting_notes import transcriber  # noqa: WPS433
    return transcriber


def test_default_device_is_cpu_with_int8(fake_faster_whisper):
    t = _transcriber_module().WhisperTranscriber("base")
    t.load_model()

    assert fake_faster_whisper._loads == [("base", "cpu", "int8")]
    assert t.active_device == "cpu"


def test_explicit_cuda_uses_float16(fake_faster_whisper):
    t = _transcriber_module().WhisperTranscriber("base", device="cuda")
    t.load_model()

    assert fake_faster_whisper._loads == [("base", "cuda", "float16")]
    assert t.active_device == "cuda"


def test_auto_passes_auto(fake_faster_whisper):
    """`auto` lets faster-whisper/ctranslate2 pick the device."""
    t = _transcriber_module().WhisperTranscriber("base", device="auto")
    t.load_model()

    assert fake_faster_whisper._loads == [("base", "auto", "auto")]


def test_cuda_no_kernel_image_falls_back_to_cpu(fake_faster_whisper):
    """A CUDA-flavored load failure must trigger a CPU retry."""
    fake_faster_whisper._failures.append((
        "cuda",
        RuntimeError(
            "CUDA error: no kernel image is available for execution on the device"
        ),
    ))

    t = _transcriber_module().WhisperTranscriber("base", device="cuda")
    t.load_model()

    assert fake_faster_whisper._loads == [
        ("base", "cuda", "float16"),
        ("base", "cpu", "int8"),
    ]
    assert t.active_device == "cpu"


def test_cudnn_failure_falls_back_to_cpu(fake_faster_whisper):
    """ctranslate2's CUDA stack fails with cuDNN/cuBLAS library errors."""
    fake_faster_whisper._failures.append((
        "cuda",
        RuntimeError("Library libcudnn_ops_infer.so.8 is not found"),
    ))

    t = _transcriber_module().WhisperTranscriber("base", device="cuda")
    t.load_model()

    assert t.active_device == "cpu"


def test_auto_falls_back_when_cuda_explodes(fake_faster_whisper):
    fake_faster_whisper._failures.append((
        None,  # any device
        RuntimeError("CUDA error: no kernel image is available"),
    ))

    t = _transcriber_module().WhisperTranscriber("base", device="auto")
    t.load_model()

    assert t.active_device == "cpu"


def test_non_cuda_error_on_cuda_does_not_silently_fall_back(fake_faster_whisper):
    """We don't want to mask unrelated errors as if they were CUDA issues."""
    fake_faster_whisper._failures.append((
        "cuda",
        ValueError("unrelated boom"),
    ))

    t = _transcriber_module().WhisperTranscriber("base", device="cuda")
    with pytest.raises(ValueError, match="unrelated boom"):
        t.load_model()


def test_unknown_device_string_falls_back_to_cpu(fake_faster_whisper):
    t = _transcriber_module().WhisperTranscriber("base", device="hocus-pocus")
    assert t.requested_device == "cpu"


def test_transcribe_builds_result_from_segments(fake_faster_whisper, tmp_path):
    """Segments (a generator in faster-whisper) become a TranscriptResult."""
    fake_faster_whisper._segments = [
        FakeSegment(0.0, 2.5, " Hello there. "),
        FakeSegment(2.5, 5.0, " General Kenobi. "),
    ]
    fake_faster_whisper._info = FakeInfo(language="en", duration=5.0)

    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"\x00\x00")

    t = _transcriber_module().WhisperTranscriber("base", device="cpu")
    result = t.transcribe(str(audio))

    assert result.text == "Hello there. General Kenobi."
    assert [s.text for s in result.segments] == ["Hello there.", "General Kenobi."]
    assert result.segments[0].start == 0.0
    assert result.segments[1].end == 5.0
    assert result.language == "en"
    assert result.duration == 5.0


def test_transcribe_empty_audio_gives_empty_result(fake_faster_whisper, tmp_path):
    fake_faster_whisper._segments = []
    fake_faster_whisper._info = FakeInfo(language="en", duration=0.0)

    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"\x00\x00")

    t = _transcriber_module().WhisperTranscriber("base", device="cpu")
    result = t.transcribe(str(audio))

    assert result.text == ""
    assert result.segments == []
    assert result.duration == 0.0


def test_transcribe_missing_file_raises(fake_faster_whisper, tmp_path):
    t = _transcriber_module().WhisperTranscriber("base", device="cpu")
    with pytest.raises(FileNotFoundError):
        t.transcribe(str(tmp_path / "nope.wav"))


def test_language_pin_passed_through(fake_faster_whisper, tmp_path):
    """whisper_language pins decoding; empty string means auto (None).
    Belt-and-suspenders with VAD against wrong-language transcription
    (ported from omdenton's fork)."""
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"\x00\x00")

    t = _transcriber_module().WhisperTranscriber("base", device="cpu", language="en")
    t.transcribe(str(audio))
    _, kwargs = t.model.transcribe_calls[0]
    assert kwargs.get("language") == "en"

    t2 = _transcriber_module().WhisperTranscriber("base", device="cpu", language="")
    t2.transcribe(str(audio))
    _, kwargs2 = t2.model.transcribe_calls[0]
    assert kwargs2.get("language") is None


def test_transcribe_uses_vad_filter(fake_faster_whisper, tmp_path):
    """VAD must be on: silence at meeting start otherwise poisons language
    detection (observed live: silent first 30s -> 'nn' -> hallucinated
    'Thank you for watching' loops for the whole file)."""
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"\x00\x00")

    t = _transcriber_module().WhisperTranscriber("base", device="cpu")
    t.transcribe(str(audio))

    _, kwargs = t.model.transcribe_calls[0]
    assert kwargs.get("vad_filter") is True
    # Default pins English (config whisper_language, "" = auto-detect).
    assert kwargs.get("language") == "en"
