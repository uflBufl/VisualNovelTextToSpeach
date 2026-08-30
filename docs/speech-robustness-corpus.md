# Speech robustness corpus

VNTTS treats a complete MOSS render as a candidate, not as proof that the WAV
is pleasant or faithful. The robustness corpus preserves exact human listening
evidence so future artifact/content checks can be measured before they are
allowed to affect publication.

## Contract

`vntts-pregenerate speech-robustness-corpus` consumes immutable cohort decision
documents and optional stable failure workspaces. Corpus v2 also binds exact
requested queue text, speaker, voice character and queue SHA-256 for
content/timing evaluation. Corpus v3 additionally retains the sorted human
defect reasons from version-2 cohort decisions. Version-1 and version-2 corpora
remain fully readable; their older `bad` labels are not guessed into new
categories. For every explicit
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

Run the optional local ASR comparison without configuring a model path:

```bash
uv run --no-sync vntts-pregenerate speech-robustness-asr \
  /path/to/v2-corpus \
  --output /path/to/asr-report.json --device cpu
```

The first online run downloads only the exact allowlisted files from pinned
`openai/whisper-tiny.en` revision
`87c7102498dcde7456f24cfd30239ca606ed9063`, verifies model-tree SHA-256
`d69d7c69a342b4cf4274fe974559249fdb240d14813cd7d03cb9094955a7240b`
and atomically publishes it under the VNTTS authoring data directory. The
managed installation records the Hugging Face snapshot's Apache-2.0 notice and
the upstream OpenAI Whisper MIT notice. Inspect it without network access with:

```bash
uv run --no-sync vntts-pregenerate asr-model-status
```

Use `--offline` on `speech-robustness-asr` to require that verified installation
and prohibit a download. An existing snapshot can be imported without network:

```bash
uv run --no-sync vntts-pregenerate asr-model-install \
  --source /path/to/openai-whisper-tiny.en-snapshot
```

The former explicit positional model path remains supported for experiments,
but is no longer part of the normal operator flow. A corrupted or partial
managed installation is never overwritten automatically; status reports the
integrity failure so the operator can inspect and remove that exact model
directory. The installer is authoring-only. Pack publication embeds semantic
evidence, and game-pack import/live playback neither imports the installer nor
requires Whisper or network access.

The command maintains a checksum-bound `.progress.json` beside the requested
output and resumes its exact completed prefix after interruption. The final
report records model-tree SHA-256 plus word substitutions, insertions,
deletions and WER. ASR errors are evidence-scored diagnostics, never direct
publication authority.

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

Corpus v2
`fae25dc41b80c58a09644fec2b514fce3e68e9f8306113e2c4f49b657f783925`
adds requested text and proportional word-position timing diagnostics. That
initial heuristic marked 9 fast and 2 unmatched-pause candidates; all 11 were
human-acceptable and none of the 13 human-bad WAVs were marked. It therefore
also remains diagnostic-only and explicitly identifies its alignment as
`proportional_word_position_without_asr` rather than claiming forced alignment.

The first complete local Whisper `tiny.en` pass produced immutable report
`124cce4df545bfa0d4c4c62b4ef7e8a57738b88a3f074e7b908bd0b8f17a49d9`
for all 178 WAVs. Its exact report and resumable progress authority live outside
the corpus under `authoring/robustness-reports`. The model was loaded from one
offline tree whose SHA-256 is recorded in the report; CPU was used because MPS
was unavailable in the active runtime.

| Provider and human verdict | Count | Median WER | Mean WER |
| --- | ---: | ---: | ---: |
| MOSS acceptable | 62 | 10.642857 | 13.176252 |
| MOSS bad | 5 | 3.208333 | 6.970581 |
| Pocket acceptable | 103 | 0.041667 | 0.076901 |
| Pocket bad | 8 | 0.218750 | 0.367188 |

This is another negative production-gate result. Whisper frequently expanded
accepted MOSS audio into long repeated transcripts. A post-hoc MOSS threshold
of WER >= 2 found 4/5 bad WAVs but also falsely marked 43/62 accepted WAVs. For
Pocket, the best exploratory WER >= 0.1875 split found 5/8 bad WAVs and falsely
marked 12/103 accepted WAVs, but the threshold was selected on the same tiny
eight-bad-WAV sample and three human-bad Pocket WAVs had exact transcripts.
Those exact-transcript failures demonstrate that content ASR cannot detect
every pacing, timbre or artifact defect even when transcription succeeds.
Neither threshold is production authority.

New cohort decisions and future corpus-v3 publications preserve explicit
independent human defect reasons for
pacing, repetition, truncation, pronunciation, timbre/artifact and speaker
identity. Existing evidence predates that schema and therefore remains honestly
unclassified. The next detector work needs a larger reason-labelled bad sample
before evaluating a stronger ASR/forced aligner on a held-out split. The safe
routing sequence remains MOSS candidate -> eligible sentence repair -> one
bounded provider-local retry -> typed per-line XTTS or Pocket fallback.
Approved WAVs are immutable and excluded from regeneration.
