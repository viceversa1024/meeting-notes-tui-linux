"""Tests for OpenAI cloud transcription (gpt-4o-transcribe-diarize).

No network: the OpenAI client is faked on the instance, ffmpeg compression
is only exercised in one real (skipped-if-missing) integration test.
See docs/superpowers/specs/2026-07-16-cloud-transcription-design.md.
"""

from __future__ import annotations

import shutil
import sys
import types
import wave
from dataclasses import dataclass

import pytest

from meeting_notes.config import AppConfig, validate_config


@dataclass
class FakeApiSegment:
    speaker: str
    text: str
    start: float
    end: float


class FakeApiResponse:
    def __init__(self, segments, language="en"):
        self.segments = segments
        self.language = language


def _write_wav(path, seconds=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)


class FakeLocalTranscriber:
    """Stands in for WhisperTranscriber as the fallback."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path, progress_callback=None):
        self.calls.append(audio_path)
        from meeting_notes.transcriber import TranscriptResult
        return TranscriptResult(
            text="local fallback text", segments=[], language="en", duration=1.0
        )


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    """An OpenAITranscriber with fake client + fake compression."""
    # The real openai package may not be importable in minimal envs; the
    # lazy import must be satisfiable either way.
    monkeypatch.setitem(sys.modules, "openai", types.ModuleType("openai"))
    sys.modules["openai"].OpenAI = lambda api_key: None

    from meeting_notes.transcriber import OpenAITranscriber

    t = OpenAITranscriber(api_key="sk-test", fallback=FakeLocalTranscriber())
    # Skip real ffmpeg: pretend compression returns the input path.
    monkeypatch.setattr(t, "_compress", lambda src, dst_dir: src)
    return t


def _install_fake_client(t, response=None, error=None):
    calls = []

    class _Transcriptions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return response

    class _Audio:
        transcriptions = _Transcriptions()

    class _Client:
        audio = _Audio()

    t._client = _Client()
    return calls


def test_diarized_segments_map_to_result(cloud, tmp_path):
    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([
        FakeApiSegment("A", " Hello there. ", 0.0, 2.0),
        FakeApiSegment("B", " Hi! ", 2.0, 3.5),
    ]))

    result = cloud.transcribe(str(audio))

    assert result.text == "Hello there. Hi!"
    assert [s.speaker for s in result.segments] == ["A", "B"]
    assert result.segments[0].start == 0.0
    assert result.duration == 3.5
    assert result.language == "en"
    # API called with the documented contract
    assert calls[0]["model"] == "gpt-4o-transcribe-diarize"
    assert calls[0]["response_format"] == "diarized_json"
    assert calls[0]["chunking_strategy"] == "auto"


def test_api_error_falls_back_to_local(cloud, tmp_path):
    audio = tmp_path / "m.wav"
    _write_wav(audio)
    _install_fake_client(cloud, error=RuntimeError("api down"))

    result = cloud.transcribe(str(audio))

    assert result.text == "local fallback text"
    assert cloud.fallback.calls == [str(audio)]


def test_oversized_compressed_file_falls_back(cloud, tmp_path, monkeypatch):
    audio = tmp_path / "m.wav"
    _write_wav(audio)
    _install_fake_client(cloud, response=FakeApiResponse([]))
    monkeypatch.setattr(
        "meeting_notes.transcriber.OpenAITranscriber._MAX_UPLOAD_BYTES", 1
    )

    result = cloud.transcribe(str(audio))

    assert result.text == "local fallback text"


def test_missing_file_raises_not_falls_back(cloud, tmp_path):
    with pytest.raises(FileNotFoundError):
        cloud.transcribe(str(tmp_path / "nope.wav"))
    assert cloud.fallback.calls == []


def test_voice_library_sent_as_data_urls(cloud, tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    _write_wav(voices / "Alex.wav", seconds=2)
    _write_wav(voices / "Blake.wav", seconds=2)
    cloud.voices_dir = str(voices)

    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([
        FakeApiSegment("Alex", "hello", 0.0, 1.0),
    ]))

    result = cloud.transcribe(str(audio))

    names = calls[0]["known_speaker_names"]
    refs = calls[0]["known_speaker_references"]
    assert sorted(names) == ["Alex", "Blake"]
    assert len(refs) == 2
    assert all(r.startswith("data:audio/wav;base64,") for r in refs)
    assert result.segments[0].speaker == "Alex"


def test_voice_library_caps_at_four_most_recent(cloud, tmp_path):
    import os

    voices = tmp_path / "voices"
    voices.mkdir()
    for i, name in enumerate(["a", "b", "c", "d", "e", "f"]):
        p = voices / f"{name}.wav"
        _write_wav(p, seconds=2)
        os.utime(p, (1000 + i, 1000 + i))  # f is newest, a oldest
    cloud.voices_dir = str(voices)

    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([]))

    cloud.transcribe(str(audio))

    assert sorted(calls[0]["known_speaker_names"]) == ["c", "d", "e", "f"]


def test_voice_library_mime_by_extension(cloud, tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "Blake.mp3").write_bytes(b"fake-mp3")
    (voices / "notes.txt").write_text("not audio")  # ignored
    cloud.voices_dir = str(voices)

    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([]))

    cloud.transcribe(str(audio))

    assert calls[0]["known_speaker_names"] == ["Blake"]
    assert calls[0]["known_speaker_references"][0].startswith("data:audio/mpeg;base64,")


def test_out_of_range_wav_clips_are_skipped(cloud, tmp_path):
    """One bad library clip must not 400 every cloud transcription
    (API accepts 1.2-10.0s references)."""
    voices = tmp_path / "voices"
    voices.mkdir()
    _write_wav(voices / "TooLong.wav", seconds=12)
    _write_wav(voices / "TooShort.wav", seconds=1)
    _write_wav(voices / "Alex.wav", seconds=5)
    cloud.voices_dir = str(voices)

    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([]))

    cloud.transcribe(str(audio))

    assert calls[0]["known_speaker_names"] == ["Alex"]


def test_missing_voices_dir_transcribes_without_references(cloud, tmp_path):
    cloud.voices_dir = str(tmp_path / "gone")

    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([]))

    cloud.transcribe(str(audio))

    assert "known_speaker_names" not in calls[0]
    assert "known_speaker_references" not in calls[0]


def test_cloud_language_pin_passed_when_set(cloud, tmp_path):
    audio = tmp_path / "m.wav"
    _write_wav(audio)
    calls = _install_fake_client(cloud, response=FakeApiResponse([]))

    cloud.language = "en"
    cloud.transcribe(str(audio))
    assert calls[0]["language"] == "en"

    cloud.language = ""
    cloud.transcribe(str(audio))
    assert "language" not in calls[1]


def test_factory_passes_language_to_both(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from meeting_notes.transcriber import OpenAITranscriber, create_transcriber

    cfg = AppConfig(
        transcription_provider="openai",
        openai_api_key="sk-test",
        whisper_language="en",
    )
    t = create_transcriber(cfg)
    assert isinstance(t, OpenAITranscriber)
    assert t.language == "en"
    assert t.fallback.language == "en"


def test_factory_passes_voices_dir(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from meeting_notes.transcriber import OpenAITranscriber, create_transcriber

    cfg = AppConfig(
        transcription_provider="openai",
        openai_api_key="sk-test",
        voices_dir="~/my-voices",
    )
    t = create_transcriber(cfg)
    assert isinstance(t, OpenAITranscriber)
    assert t.voices_dir == "~/my-voices"


def test_factory_default_is_local():
    from meeting_notes.transcriber import WhisperTranscriber, create_transcriber

    t = create_transcriber(AppConfig())
    assert isinstance(t, WhisperTranscriber)


def test_factory_openai_provider_with_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from meeting_notes.transcriber import OpenAITranscriber, WhisperTranscriber, create_transcriber

    cfg = AppConfig(transcription_provider="openai", openai_api_key="sk-test")
    t = create_transcriber(cfg)
    assert isinstance(t, OpenAITranscriber)
    assert isinstance(t.fallback, WhisperTranscriber)
    # Local fallback honors the configured whisper settings
    assert t.fallback.model_name == cfg.whisper_model


def test_factory_openai_without_key_stays_local(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from meeting_notes.transcriber import WhisperTranscriber, create_transcriber

    cfg = AppConfig(transcription_provider="openai", openai_api_key="")
    t = create_transcriber(cfg)
    assert isinstance(t, WhisperTranscriber)


def test_cloud_transcriber_formatter_works(cloud):
    """Regression: the aliased formatter must work on OpenAITranscriber too
    (staticmethod re-wrap — live run crashed with 'takes 1 positional
    argument but 2 were given')."""
    from meeting_notes.transcriber import TranscriptResult, TranscriptSegment

    result = TranscriptResult(
        text="hi",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hi", speaker="A")],
        language="en",
        duration=1.0,
    )
    assert "**[00:00] A:** hi" in cloud.format_transcript_with_timestamps(result)


def test_formatter_includes_speaker_labels():
    from meeting_notes.transcriber import (
        TranscriptResult,
        TranscriptSegment,
        WhisperTranscriber,
    )

    result = TranscriptResult(
        text="a b",
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="hello", speaker="A"),
            TranscriptSegment(start=61.0, end=62.0, text="plain"),
        ],
        language="en",
        duration=62.0,
    )
    out = WhisperTranscriber("base").format_transcript_with_timestamps(result)
    assert "**[00:00] A:** hello" in out
    assert "**[01:01]** plain" in out


def test_config_validation_accepts_known_providers():
    # ai_provider="none" so validation isolates transcription_provider
    # (other providers demand API keys that test envs don't have).
    ok, _ = validate_config(AppConfig(ai_provider="none", transcription_provider="local"))
    assert ok
    ok, _ = validate_config(AppConfig(ai_provider="none", transcription_provider="openai"))
    assert ok
    ok, err = validate_config(AppConfig(ai_provider="none", transcription_provider="deepfake"))
    assert not ok and "transcription_provider" in err


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_compress_produces_small_webm(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.ModuleType("openai"))
    sys.modules["openai"].OpenAI = lambda api_key: None
    from meeting_notes.transcriber import OpenAITranscriber

    audio = tmp_path / "m.wav"
    _write_wav(audio, seconds=2)
    t = OpenAITranscriber(api_key="sk-test", fallback=FakeLocalTranscriber())

    out = t._compress(str(audio), str(tmp_path))

    assert out.endswith(".webm")
    assert (tmp_path / out.rsplit("/", 1)[-1]).stat().st_size < audio.stat().st_size
