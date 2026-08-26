# Speech robustness corpus

VNTTS treats a complete MOSS render as a candidate, not as proof that the WAV
is pleasant or faithful. The robustness corpus preserves exact human listening
evidence so future artifact/content checks can be measured before they are
allowed to affect publication.

## Contract

`vntts-pregenerate speech-robustness-corpus` consumes immutable cohort decision
documents and optional stable failure workspaces. For every explicit
`acceptable` or `bad` assessment it binds:

- the exact workspace, queue item, state item, WAV SHA-256 and decision ID;
- provider, model, profile, voice, seed, repair and stored quality provenance;
- a self-contained copy of the exact WAV and original decision document;
- versioned waveform and speech-silence diagnostics.

Repeated listening evidence for the same workspace/queue/WAV is retained as a
set of decision IDs, while the WAV is copied once. Old decisions that recorded
only `heard` are valid review authority but are not guessed into a quality
label. Failed state items carry their typed failure record and synthesis
provenance without inventing a WAV.

The corpus is published by an atomic no-replace directory rename. Its loader
requires an exact artifact inventory, refuses symlinks and path escapes,
rechecks every checksum, decodes every PCM16 mono WAV and recomputes the stored
diagnostics. Publication rechecks all source decision, state and WAV snapshots
immediately before the final rename. The source workspaces are never mutated.

All new waveform signals are deliberately marked:

```json
{"diagnostic_only": true, "automatic_rejection": false}
```

Human labels are authority. A signal may become a production gate only after a
versioned policy demonstrates acceptable false-positive and false-negative
behavior on this corpus.

## Commands

Publish from every cohort decision below the authoring workspace root and add
the final typed failures from a stable workspace:

```bash
uv run --no-sync vntts-pregenerate speech-robustness-corpus \
  /path/to/robustness-corpus \
  --decision-root /path/to/authoring/workspaces \
  --failure-workspace /path/to/stable/workspace
```

Validate the corpus and every bound artifact without consulting the sources:

```bash
uv run --no-sync vntts-pregenerate speech-robustness-check \
  /path/to/robustness-corpus
```

An identical republish is idempotent. Changed source authority or a conflicting
destination fails closed.

## Current Character Story baseline

The first real publication on 2026-08-27 produced corpus
`40dd06ed800465a00b75aa0fa6fef014a15dff9e43327bdd3a365ef60662e9a8`
with 178 unique labelled WAVs and 15 typed failures. It is stored under the
authoring app-data `robustness-corpora/current-character-story-human-labelled-v1`
directory:

| Evidence | Count |
| --- | ---: |
| Human acceptable | 165 |
| Human bad | 13 |
| MOSS WAVs | 67 (62 acceptable, 5 bad) |
| Pocket WAVs | 111 (103 acceptable, 8 bad) |
| Missed-EOS/audio-limit failures | 8 |
| Speech-silence failures | 7 |

The corpus occupies about 69 MiB. Its initial structural diagnostics produced
five `near_clipping_candidate` signals, all on human-acceptable WAVs. None of
the 13 human-bad WAVs triggered the new exact-repeat, clipping, DC-offset or
discontinuity signals. This is a useful negative result: simply tightening
waveform thresholds would reject good audio while leaving the heard artifacts
untouched.

The next detector work therefore needs content/timing evidence, including
repetition or dropped-content comparison and pause placement relative to the
requested text. It must report each reason separately and remain diagnostic
until calibrated. The safe routing sequence remains MOSS candidate -> eligible
sentence repair -> one bounded provider-local retry -> typed per-line XTTS or
Pocket fallback. Approved WAVs are immutable and excluded from regeneration.
