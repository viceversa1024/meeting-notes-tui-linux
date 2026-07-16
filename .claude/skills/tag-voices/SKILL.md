---
name: tag-voices
description: Use when adding or fixing a speaker in the voice tag library — a transcript labels someone as a letter (A/B/...) and the user says who it is, a person's segments are mislabeled or split across labels, or a voice tag needs to be replaced or removed.
---

# Tag Voices

## Overview

The voice tag library (`voices_dir` in config, default
`~/.config/meeting-notes/voices/`) holds one short clip per person;
filename stem = the name shown in transcripts. Cloud transcriptions send
up to **4** clips as reference voices, picked by likelihood of presence:
`primary_voice` from config (the user), then people named in the meeting
title, then most recently modified. One command grows the library from
any labeled transcript — never cut audio manually:

```bash
venv/bin/python -m meeting_notes.voice_tag <transcript.txt> <label> <Name> --recording <recording.wav>
```

## Steps

1. **Verify the label really is that person before tagging.** Diarization
   splits one voice across labels and letters can be noise. Read the
   label's longest lines from the transcript — do they sound like that
   person (vocabulary, role, which side of the conversation)? If the user
   named a label but its lines look like someone else's speech, show them
   2-3 of its lines and confirm first. A wrong clip silently poisons every
   future transcript.
2. **Find the recording.** The transcript's `Recording:` header is usually
   empty; the matching wav in `recordings/` shares the transcript
   filename's timestamp prefix (fallback: match duration from the header).
3. **Run the command.** Capitalize `<Name>` — the stem is displayed as-is.
   It picks the label's longest utterance, prints the chosen line and
   timestamp, and cuts a ≤9.5s mono clip into the library.
4. **Verify.** The printed line should read like that person. The clip
   must be 1.2–10s (out-of-range clips are skipped at send time with a
   log warning). To be thorough, transcribe the clip locally and check the
   words match the printed line:
   `venv/bin/python -c "from faster_whisper import WhisperModel; m=WhisperModel('base',device='cpu',compute_type='int8'); print(' '.join(s.text for s,_ in zip(*[iter(m.transcribe('<clip>')[0])]*1)))"`
   — or simply `ffprobe` it and play it back for the user.
5. **Mind the cap.** Only 4 clips are sent per meeting: primary voice,
   title-matched names, then newest. With more than 4 tags, tell the user
   who made the cut for a typical meeting; a person named in the meeting
   title is always included, and `touch`-ing a clip bumps its recency.

## Quick reference

| Task | How |
|---|---|
| Add person from transcript | the command above |
| Remove/undo a tag | delete `voices/<Name>.wav` |
| Replace a bad tag | re-run the command (overwrites) |
| See active 4 | `ls -t <voices_dir> \| head -4` |
| Rename someone | `mv voices/Old.wav voices/New.wav` (bumps mtime → activates) |

## Common mistakes

- **Tagging a noise label** — letters with only 1-2 short lines are
  diarization junk, not a person.
- **Assuming the user's label guess is right** — check the lines; in one
  real case "B" was the user themself, and tagging it as a third party
  would have mislabeled every future meeting.
- **Cutting clips by hand with ffmpeg** — the command already picks the
  best utterance and enforces the API's duration bounds.
