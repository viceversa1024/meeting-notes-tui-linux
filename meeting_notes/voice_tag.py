"""Grow the voice tag library from labeled transcript lines.

Every transcript line carries a timestamp and (for cloud transcripts) a
speaker label. That is enough to cut a reference clip of any speaker from
the meeting recording — no manual audio editing:

    python -m meeting_notes.voice_tag <transcript> <label> <name> [--recording PATH]

e.g. after checking that "A" in a transcript is Blake:

    python -m meeting_notes.voice_tag transcripts/2026-07-16-....txt A Blake

The clip lands in the voice library (config `voices_dir`), and future cloud
transcriptions label that voice by name. See
docs/superpowers/specs/2026-07-16-voice-tag-library-design.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logger import get_logger

logger = get_logger(__name__)

# **[MM:SS] Name:** text   |   **[H:MM:SS] Name:** text   |   **[MM:SS]** text
_LINE_RE = re.compile(
    r"^\*\*\[(?:(\d+):)?(\d+):(\d+)\](?: ([^:*]+):)?\*\* ?(.*)$"
)

# The API accepts reference clips of 1.2-10.0 seconds. We cut at most 9.5s
# because ffmpeg -t can land a hair over the ask, and a 10.004s clip gets
# the whole transcription request rejected (seen live).
_MIN_CLIP_S = 2.0
_MAX_CLIP_S = 9.5


@dataclass
class LabeledLine:
    speaker: str
    start: float
    text: str
    duration: Optional[float]  # gap to the next line; None for the last line


def parse_labeled_lines(transcript: str) -> list[LabeledLine]:
    """Extract speaker-labeled lines with start times and gap durations."""
    raw = []
    for line in transcript.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        hours, minutes, seconds, speaker, text = m.groups()
        start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        raw.append((float(start), speaker, text))

    lines = []
    for i, (start, speaker, text) in enumerate(raw):
        duration = raw[i + 1][0] - start if i + 1 < len(raw) else None
        if speaker:  # unlabeled lines still bound durations, but aren't candidates
            lines.append(LabeledLine(speaker=speaker, start=start, text=text, duration=duration))
    return lines


def best_utterance(lines: list[LabeledLine], label: str) -> Optional[LabeledLine]:
    """The label's best reference clip source: longest gap, then most words."""
    candidates = [
        ln for ln in lines
        if ln.speaker == label and ln.duration is not None and ln.duration >= _MIN_CLIP_S
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda ln: (min(ln.duration, _MAX_CLIP_S), len(ln.text.split())))


def _recording_from_header(transcript: str, transcript_path: Path) -> Optional[Path]:
    for line in transcript.splitlines()[:10]:
        if line.startswith("Recording:"):
            name = line.split(":", 1)[1].strip()
            if name:
                p = Path(name)
                return p if p.is_absolute() else transcript_path.parent / p
    return None


def tag_voice(
    transcript_path: str,
    label: str,
    name: str,
    recording_path: Optional[str] = None,
    voices_dir: str = "",
) -> str:
    """Cut `label`'s best utterance from the recording into voices/<name>.wav.

    Returns the created clip path. Raises FileNotFoundError when no usable
    recording is available and ValueError when the label has no usable lines.
    """
    import subprocess

    tpath = Path(transcript_path).expanduser()
    transcript = tpath.read_text()

    recording = (
        Path(recording_path).expanduser()
        if recording_path
        else _recording_from_header(transcript, tpath)
    )
    if recording is None or not recording.is_file():
        raise FileNotFoundError(
            "Recording not found — pass --recording (the transcript header "
            f"gave {recording or 'nothing'})"
        )

    lines = parse_labeled_lines(transcript)
    best = best_utterance(lines, label)
    if best is None:
        known = sorted({ln.speaker for ln in lines})
        raise ValueError(
            f"No usable (>= {_MIN_CLIP_S:.0f}s) lines for label {label!r}; "
            f"labels in this transcript: {known}"
        )

    if not voices_dir:
        from .config import load_config
        voices_dir = load_config().voices_dir
    lib = Path(voices_dir).expanduser()
    lib.mkdir(parents=True, exist_ok=True)
    out = lib / f"{name}.wav"

    clip_len = min(best.duration, _MAX_CLIP_S)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(best.start), "-t", str(clip_len),
         "-i", str(recording), "-ac", "1", "-ar", "16000", str(out)],
        check=True, capture_output=True,
    )

    logger.info(f"voice tag saved: {out} ({clip_len:.1f}s from {recording.name})")
    print(f"Tagged {name!r} from [{int(best.start // 60):02d}:{int(best.start % 60):02d}] "
          f"({clip_len:.1f}s): \"{best.text[:80]}\"")
    print(f"Saved: {out}")
    return str(out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("transcript", help="transcript .txt with **[MM:SS] X:** lines")
    parser.add_argument("label", help="speaker label to tag (e.g. A)")
    parser.add_argument("name", help="person's name (becomes voices/<name>.wav)")
    parser.add_argument("--recording", help="recording path (default: transcript header)")
    args = parser.parse_args()

    tag_voice(args.transcript, args.label, args.name, recording_path=args.recording)
