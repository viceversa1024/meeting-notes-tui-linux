"""Transcription module using faster-whisper (CTranslate2).

faster-whisper runs Whisper models ~4x faster than openai-whisper on CPU
with int8 quantization, and needs no PyTorch at all. We default to CPU to
match the README's "CPU-based, privacy-first" promise, allow opt-in CUDA via
config, and transparently fall back to CPU if the chosen device can't
actually load the model (missing cuDNN/cuBLAS, no kernel image, etc.).
"""

from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass

from .logger import get_logger

logger = get_logger(__name__)


@dataclass
class TranscriptSegment:
    """A segment of transcribed text with timing information."""
    start: float
    end: float
    text: str
    speaker: Optional[str] = None


@dataclass
class TranscriptResult:
    """Complete transcription result."""
    text: str
    segments: list[TranscriptSegment]
    language: str
    duration: float
    # Which backend/model actually produced this result (a cloud request
    # that fell back to local reports the local model).
    model: str = ""


_VALID_DEVICES = ("auto", "cpu", "cuda")

# compute type per device: int8 is the fast CPU path with near-fp32 quality;
# float16 is the standard CUDA path. "auto" lets ctranslate2 decide.
_COMPUTE_TYPES = {"cpu": "int8", "cuda": "float16", "auto": "auto"}


def _looks_like_cuda_failure(err: BaseException) -> bool:
    """Heuristic: does this exception indicate the CUDA path is unusable?"""
    msg = f"{type(err).__name__}: {err}"
    needles = (
        "no kernel image is available",
        "CUDA error",
        "CUDA driver",
        "cudaError",
        "device-side assert",
        # ctranslate2 raises these when the CUDA libraries are missing/broken
        "cudnn",
        "cublas",
        "CUDA is not available",
        "no CUDA device",
    )
    return any(n.lower() in msg.lower() for n in needles)


class WhisperTranscriber:
    """Transcribe audio files using faster-whisper."""

    def __init__(self, model_name: str = "base", device: str = "cpu", language: str = "en"):
        """Initialize the transcriber.

        Args:
            model_name: Whisper model to use (tiny, base, small, medium, large)
            device: One of ``"cpu"``, ``"cuda"``, or ``"auto"``. Defaults to
                ``"cpu"`` because that matches the documented privacy-first
                CPU pipeline and avoids broken CUDA installs taking the app
                down. ``"auto"`` lets ctranslate2 pick (CUDA when available)
                but still falls back to CPU on load failure.
            language: Language code (e.g. ``"en"``) to pin decoding to;
                empty string auto-detects. Belt-and-suspenders with VAD: a
                quiet/noisy opening can still misdetect and mistranscribe
                the whole meeting (ported from omdenton's fork).
        """
        if device not in _VALID_DEVICES:
            logger.warning(f"Unknown whisper device {device!r}, falling back to 'cpu'")
            device = "cpu"
        logger.info(f"Initializing WhisperTranscriber (model: {model_name}, device: {device})")
        self.model_name = model_name
        self.requested_device = device
        self.language = language
        self.active_device: Optional[str] = None
        self.model = None  # type: ignore[assignment]

    def _load_on(self, device: str):
        from faster_whisper import WhisperModel  # noqa: WPS433 (lazy import)

        return WhisperModel(
            self.model_name,
            device=device,
            compute_type=_COMPUTE_TYPES[device],
        )

    def load_model(self):
        """Load the Whisper model (lazy loading), with CUDA-failure fallback."""
        if self.model is not None:
            return

        target = self.requested_device
        try:
            logger.info(f"Loading Whisper {self.model_name} model (device={target})...")
            self.model = self._load_on(target)
            # ctranslate2 exposes the resolved device for "auto"
            self.active_device = str(getattr(self.model, "device", target))
            logger.info(f"Whisper model loaded successfully on {self.active_device}")
            return
        except Exception as exc:  # noqa: BLE001 - handle anything ctranslate2 throws
            if target == "cpu" or not _looks_like_cuda_failure(exc):
                logger.error(f"Whisper model load failed: {exc}", exc_info=True)
                raise

            logger.warning(
                f"Whisper failed to load on {target} ({exc}). Falling back to CPU."
            )
            try:
                self.model = self._load_on("cpu")
                self.active_device = "cpu"
                logger.info("Whisper model loaded successfully on cpu (after CUDA failure)")
            except Exception as cpu_exc:
                logger.error(f"CPU fallback also failed: {cpu_exc}", exc_info=True)
                raise

    def transcribe(
        self,
        audio_path: str,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> TranscriptResult:
        """Transcribe an audio file."""
        logger.info(f"Starting transcription: {audio_path}")
        self.load_model()

        audio_file = Path(audio_path)
        if not audio_file.exists():
            logger.error(f"Audio file not found: {audio_file}")
            raise FileNotFoundError(f"Audio file not found: {audio_file}")

        file_size_mb = audio_file.stat().st_size / (1024 * 1024)
        logger.info(f"Transcribing {audio_file.name} ({file_size_mb:.1f} MB)...")

        if self.model is None:
            logger.error("Model not loaded")
            raise RuntimeError("Model not loaded")

        # vad_filter strips non-speech before language detection and
        # decoding. Without it, a silent meeting start poisons language
        # detection (first 30s window) and Whisper hallucinates filler like
        # "Thank you for watching" across the whole file.
        raw_segments, info = self.model.transcribe(
            str(audio_file),
            language=self.language or None,
            task="transcribe",
            vad_filter=True,
        )

        # faster-whisper returns a generator; decoding happens as we iterate.
        segments = [
            TranscriptSegment(start=seg.start, end=seg.end, text=seg.text.strip())
            for seg in raw_segments
        ]

        duration = segments[-1].end if segments else 0.0

        logger.info(
            f"Transcription complete: {len(segments)} segments, "
            f"{duration:.1f}s duration, language: {info.language}"
        )

        return TranscriptResult(
            text=" ".join(s.text for s in segments if s.text),
            segments=segments,
            language=getattr(info, "language", None) or "unknown",
            duration=duration,
            model=f"faster-whisper {self.model_name}",
        )

    def format_transcript_with_timestamps(self, result: TranscriptResult) -> str:
        """Format transcript with timestamps (and speakers, when known)."""
        lines = []
        for seg in result.segments:
            timestamp = self._format_timestamp(seg.start)
            if seg.speaker:
                lines.append(f"**[{timestamp}] {seg.speaker}:** {seg.text}")
            else:
                lines.append(f"**[{timestamp}]** {seg.text}")
        return "\n\n".join(lines)

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """Format seconds as HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"


class OpenAITranscriber:
    """Cloud transcription via OpenAI ``gpt-4o-transcribe-diarize``.

    Same public surface as WhisperTranscriber; segments carry speaker
    labels. Compression (mono 16 kbps Opus/WebM) keeps uploads far under
    the API's 25 MB cap. Any failure — ffmpeg, upload, API, parsing —
    falls back to the local WhisperTranscriber so a meeting is never lost
    to a network problem.
    """

    _MODEL = "gpt-4o-transcribe-diarize"
    _MAX_UPLOAD_BYTES = 25 * 1024 * 1024

    # API cap on known_speaker_references
    _MAX_KNOWN_SPEAKERS = 4
    _VOICE_MIMES = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
    }

    def __init__(
        self,
        api_key: str,
        fallback: "WhisperTranscriber",
        voices_dir: str = "",
        language: str = "en",
    ):
        logger.info("Initializing OpenAITranscriber (cloud, diarized)")
        self.api_key = api_key
        self.fallback = fallback
        self.voices_dir = voices_dir
        self.language = language
        self._client = None

    @staticmethod
    def _clip_duration_ok(clip: Path) -> bool:
        """API accepts 1.2-10.0s references; one bad clip 400s the whole
        request, so out-of-range .wav clips are skipped up front."""
        if clip.suffix.lower() != ".wav":
            return True  # can't check cheaply; let the API judge
        import wave

        try:
            with wave.open(str(clip), "rb") as w:
                seconds = w.getnframes() / w.getframerate()
        except Exception:  # noqa: BLE001 - non-PCM wav etc.; let the API judge
            return True
        if 1.2 <= seconds <= 10.0:
            return True
        logger.warning(
            f"voice tag {clip.name} is {seconds:.1f}s (API needs 1.2-10.0s) — skipped"
        )
        return False

    def _speaker_kwargs(self) -> dict:
        """known-speaker params from the voice library, {} if unusable.

        Sends the 4 most recently modified clips (API cap) so recently
        tagged people stay resolvable. Never fails the transcription.
        """
        if not self.voices_dir:
            return {}
        lib = Path(self.voices_dir).expanduser()
        if not lib.is_dir():
            return {}

        clips = sorted(
            (p for p in lib.iterdir() if p.suffix.lower() in self._VOICE_MIMES),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[: self._MAX_KNOWN_SPEAKERS]

        import base64

        names, refs = [], []
        for clip in clips:
            if not self._clip_duration_ok(clip):
                continue
            try:
                data = base64.b64encode(clip.read_bytes()).decode("ascii")
            except OSError as exc:
                logger.warning(f"voice tag {clip} unreadable ({exc}) — skipped")
                continue
            names.append(clip.stem)
            refs.append(f"data:{self._VOICE_MIMES[clip.suffix.lower()]};base64,{data}")

        if not names:
            return {}
        logger.info(f"Cloud transcription: sending voice tags for {names}")
        return {"known_speaker_names": names, "known_speaker_references": refs}

    def load_model(self):
        """No-op: kept for interface parity with WhisperTranscriber."""

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # noqa: WPS433 (lazy import)
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def _compress(self, src: str, dst_dir: str) -> str:
        """Compress to mono 16 kbps Opus in WebM (an API-supported container)."""
        import subprocess

        dst = str(Path(dst_dir) / (Path(src).stem + ".webm"))
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src,
             "-ac", "1", "-c:a", "libopus", "-b:a", "16k", dst],
            check=True, capture_output=True,
        )
        return dst

    def transcribe(
        self,
        audio_path: str,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> TranscriptResult:
        """Transcribe in the cloud; fall back to local on any failure."""
        audio_file = Path(audio_path)
        if not audio_file.exists():
            logger.error(f"Audio file not found: {audio_file}")
            raise FileNotFoundError(f"Audio file not found: {audio_file}")

        import tempfile

        try:
            with tempfile.TemporaryDirectory(prefix="meeting-notes-cloud-") as tmp:
                logger.info(f"Cloud transcription: compressing {audio_file.name}...")
                upload = Path(self._compress(str(audio_file), tmp))
                size = upload.stat().st_size
                if size > self._MAX_UPLOAD_BYTES:
                    raise ValueError(
                        f"Compressed audio is {size / 1e6:.0f} MB, over the API cap"
                    )

                logger.info(
                    f"Cloud transcription: uploading {size / 1e6:.1f} MB to {self._MODEL}..."
                )
                lang_kwargs = {"language": self.language} if self.language else {}
                with open(upload, "rb") as f:
                    resp = self._get_client().audio.transcriptions.create(
                        file=f,
                        model=self._MODEL,
                        response_format="diarized_json",
                        chunking_strategy="auto",
                        **lang_kwargs,
                        **self._speaker_kwargs(),
                    )

            segments = [
                TranscriptSegment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text.strip(),
                    speaker=getattr(seg, "speaker", None),
                )
                for seg in resp.segments
            ]
            duration = segments[-1].end if segments else 0.0
            logger.info(
                f"Cloud transcription complete: {len(segments)} segments, "
                f"{duration:.1f}s, speakers: {sorted({s.speaker for s in segments if s.speaker})}"
            )
            return TranscriptResult(
                text=" ".join(s.text for s in segments if s.text),
                segments=segments,
                language=getattr(resp, "language", None) or "unknown",
                duration=duration,
                model=self._MODEL,
            )
        except Exception as exc:  # noqa: BLE001 - fallback must catch everything
            logger.warning(
                f"Cloud transcription failed ({exc}). Falling back to local whisper."
            )
            return self.fallback.transcribe(audio_path, progress_callback)

    # Same formatting logic as the local transcriber. The staticmethod
    # re-wrap matters: class-body access returns the bare function, which
    # would otherwise bind as an instance method and swallow `self`.
    format_transcript_with_timestamps = WhisperTranscriber.format_transcript_with_timestamps
    _format_timestamp = staticmethod(WhisperTranscriber._format_timestamp)


def create_transcriber(config):
    """Build the transcriber the config asks for.

    ``transcription_provider: openai`` needs an OpenAI key (config or env);
    otherwise we quietly stay on the local pipeline.
    """
    import os

    if config.transcription_provider == "openai":
        api_key = config.openai_api_key or os.getenv("OPENAI_API_KEY")
        if api_key:
            return OpenAITranscriber(
                api_key=api_key,
                fallback=WhisperTranscriber(
                    config.whisper_model,
                    device=config.whisper_device,
                    language=config.whisper_language,
                ),
                voices_dir=config.voices_dir,
                language=config.whisper_language,
            )
        logger.warning(
            "transcription_provider is 'openai' but no OpenAI API key is "
            "configured — using local transcription"
        )
    return WhisperTranscriber(
        config.whisper_model,
        device=config.whisper_device,
        language=config.whisper_language,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python transcriber.py <audio_file>")
        sys.exit(1)

    transcriber = WhisperTranscriber()
    result = transcriber.transcribe(sys.argv[1])

    print(f"\nLanguage: {result.language}")
    print(f"Duration: {result.duration:.1f}s")
    print(f"\nTranscript:\n{result.text}")
    print(f"\nWith timestamps:\n{transcriber.format_transcript_with_timestamps(result)}")
