# Pregeneration coverage plan

Pregeneration should maximize usable offline speech before live play while
preserving speaker identity, source-audio candidates and exact synthesis
provenance. Coverage is not permission to assign a merely similar voice, raise
the bounded audio limit until a render completes, or publish a failed result.

## Current Character Story evidence

The read-only 2026-08-18 snapshot of workspace
`resume-395a5e5eec0327a3a793b66d-4b727b4671cb0ba2` has 585 spoken queue items:

- 69 approved, including 59 Narrator/Centurion and ten Rhiannon artifacts;
- 61 generated Narrator artifacts still awaiting review and seven explicit
  rejections;
- 211 failed items: 167 audio-limit/missed-EOS and 44 speech-silence failures;
- 237 lines blocked by missing voice references; and
- seven pure sound effects excluded from speech generation.

The exact state SHA-256 is
`10ad11119a57fda8ffccf37f31e1a949ee03d4bbd1092c99d867c36be866b875` and
the approved-only manifest SHA-256 is
`6147c5d5560275f97f617e0c214ce001de3677b3d4f6467a804bafeeec9dc461`.
These identify this planning snapshot, not a frozen future review state.

Missing from the selected voice manifest does not mean absent from the game.
The complete story index already contains installed same-speaker audio for six
of the nine blocked roles:

| Role | Blocked lines | Installed same-speaker evidence |
| --- | ---: | --- |
| Aderyn | 113 | `activityvoc_story_hero3146_beiai.bnk` and `activityvoc_story_npcnoname326_beiai.bnk`; multiple portrait/age variants require separate auditioned reference groups |
| Dobharchú | 50 | `activityvoc_story_npcnoname323_beiai.bnk` |
| Mrs. Owen | 34 | `activityvoc_story_npcnoname322_beiai.bnk` |
| Hotelier | 12 | `activityvoc_story_npcnoname327_beiai.bnk` |
| Poacher | 4 | `activityvoc_story_npcnoname325_beiai.bnk` |
| Aderyn's Father | 1 | `activityvoc_story_npcnoname324_beiai.bnk` |

Those candidates cover 214 blocked lines if their bank/media hashes, speaker
identity, portrait grouping and listening checks pass. `Poacher I` (13 lines)
and `Poacher II` (nine lines) have no exact installed same-speaker route in the
current story index. Glyndŵr's one line points only to configured-unavailable
audio. These 23 lines are the current evidence boundary for an explicit
fallback rather than an inferred character voice.

## Coverage order

Pregeneration follows a fixed order so a fallback cannot hide recoverable game
audio or a missed extraction path:

1. Preserve and route exact installed source audio where the game supplies it.
2. Mine the full story index, playable-character config, portrait/NPC mapping
   and installed bank index for same-speaker reference candidates.
3. Import, checksum, score and listen to compact reference clips. Keep distinct
   portrait/bank voice variants separate until listening proves they are one
   performer and presentation.
4. Generate with the selected primary backend and immutable model/profile,
   reference and text controls.
5. Apply bounded, cohort-specific repairs to failures.
6. Pregenerate the remaining eligible cohort with an explicitly configured
   offline fallback backend when the primary backend still cannot complete.
7. Use an explicit Narrator fallback only for roles that remain unresolved
   after the source audit.
8. Publish only exact source or approved generated artifacts. Retain an
   explicit live-fallback decision for genuinely unresolved lines rather than
   pretending a failed render is complete.

## Explicit unknown-role Narrator policy

The default missing-voice policy is `block`. A config-addressed workspace may
opt into `narrator_roles` for an exact list or
`narrator_all_unresolved` for every still-unresolved named role. The CLI carries
those choices as repeated `--narrator-fallback-role` values or the deliberately
broad `--narrator-fallback-all` flag. Each affected state and manifest record
retains:

- original `speaker` and requested `voice_character`;
- effective `synthesis_character=Narrator` and the selected narrator character;
- the fallback policy/version and reason;
- exact narrator reference and model/profile provenance; and
- the original line/text identity.

The workbench must display source role and effective synthesis voice
separately. This makes `Poacher I -> Narrator -> Centurion` visible instead of
claiming that Centurion is Poacher I.

The policy document and exact requested-to-effective mapping are included in
the synthesis-control digest. A changed policy creates a different immutable
workspace. Generation fails closed if a requested override is not authorized,
if it targets anything other than Narrator, or if the selected narrator has no
bound reference. The workbench review table already presents source `Speaker`
and effective `Character` separately, while its header identifies the selected
Narrator character. Old workspaces without this field read as `block` and are
never broadened in place.

Canonical generated speech should contain only the story text. Automatically
prepending phrases such as "Poacher I says" changes the script, duplicates the
visible nameplate and makes ordinary dialogue unnatural. If spoken speaker
identification is needed for accessibility, implement it as a separate,
optional `announce speaker on change` route with its own provenance and
acceptance test; do not bake it into canonical story WAVs.

## Failure cohorts and preventive generation

Failure handling must use typed state fields rather than parsing volatile error
strings. At minimum, persist `missed_eos_audio_limit`, `speech_silence`,
`reference_unavailable`, `backend_error`, `cancelled` and `interrupted`, plus
the backend/model/profile, reference digest, attempt/seed, word and punctuation
features, audio/token limit utilization and measured silence spans.

The current 167 limit and 44 silence failures need separate treatment:

- For missed EOS, compare word count, punctuation, stutters, ellipses, attempt
  seeds and exact limit utilization. Keep the 20-second safety ceiling. Use a
  small bounded seed cohort, and stop when its completion yield is poor.
- For long or punctuation-heavy text, test sentence-boundary segmentation and
  checksum-bound concatenation with a deliberate pause. Never split inside a
  phrase or reintroduce token-level duration control; listening already found
  that forced mid-phrase timing creates bad pauses.
- For silence failures, distinguish intentional sentence pauses from leading,
  trailing or hallucinated internal silence. Compare reference choice and seed;
  trim only safe edge silence and never delete semantic internal timing merely
  to pass the gate.
- Revalidate short ellipsis/stutter normalization and pure-SFX classification
  before synthesis so non-speech or incomplete text does not consume retries.
- When bounded MOSS repairs still fail, render the exact cohort offline with
  Pocket TTS using the chosen role or Narrator reference. Record the different
provider/model and nullable provider-specific seed semantics per item. A
Pocket artifact must pass the same WAV, silence, checksum and manual-review
gates as a MOSS artifact.

Current VNTTS generation writes a versioned `failure` record for every new
failed attempt. It includes the stable kind, exact text-shape features and, when
available, typed completion limits/utilization or measured silence spans. The
read-only report also projects historical string-only states into the same
taxonomy without rewriting their authoritative bytes:

```bash
uv run vntts-pregenerate failure-report \
  --state WORKSPACE/generated-audio/generation-state.json \
  --queue WORKSPACE/queue.jsonl > failure-report.json
```

The report groups records by kind, source/effective role, backend/model/profile,
synthesis-control digest, attempt/seed, text shape, limit utilization and
silence measurements. Against the current Character Story snapshot it
reconciles all 211 failures exactly as 167 `missed_eos_audio_limit` and 44
`speech_silence`: 186 Narrator, 16 Rhiannon and nine unattributed-source lines
whose established effective voice is Narrator. This classification is a retry
planning input, not permission to regenerate a whole low-yield cohort.

Before changing state, derive an exact-ID repair plan:

```bash
uv run vntts-pregenerate failure-repair-plan \
  --state WORKSPACE/generated-audio/generation-state.json \
  --queue WORKSPACE/queue.jsonl > failure-repair-plan.json
```

The read-only versioned plan never starts synthesis. It separates
multiple-sentence limit failures for sentence-boundary segmentation, low-attempt
limit failures for a bounded seed retry, exhausted limits for the offline
fallback backend, edge-only measured silence for trim-and-listen review, other
silence for reference comparison, unavailable references for source discovery,
cancelled/interrupted work for safe resume and remaining backend failures for
manual diagnosis. Every recommendation carries the exact queue ID and current
state/queue hashes, so later execution can require a fresh authority snapshot
instead of mass retrying a stale cohort.

Three conservative actions now have an executable, config-addressed path. Create
a new workspace with repeated `--sentence-segment-failed QUEUE_ID` and/or
`--trim-edge-silence-failed QUEUE_ID`, or
`--bounded-seed-failed QUEUE_ID`. The generated child command carries the same
exact queue IDs. Before any render, bulk generation requires the selected ID
set to equal the repair policy, reloads authoritative state, requires a current
typed `failed` outcome and verifies that its failure kind and metrics still
match the requested strategy. Historical string-only failures remain planning
evidence and must first produce a typed current failure; they are not silently
authorized.

Sentence repair uses only complete, substantial sentence boundaries. Each
segment is an independent typed render with a deterministic successive seed;
complete results are concatenated with a bounded 180 ms pause. The original
queue text, every segment and segment text SHA-256, planned segment seeds,
pause, provider controls and outer attempt/seed remain in state and the approved manifest. Edge repair is
available only for a typed speech-silence failure whose excessive silence is
strictly leading and/or trailing, never internal. It retains 80 ms boundary
padding, then runs the ordinary WAV and speech-quality gates. All three paths refuse
an obsolete cohort and never publish a limited, cancelled or still-invalid
result.

Bounded seed repair is available only for a typed missed-EOS/audio-limit
failure with fewer than three cumulative attempts. It uses the existing
deterministic cumulative seed rule and clamps the run to at most three total
attempts even if a caller supplies a larger `--retries` value. Reopening the
policy after the third failed attempt is rejected without changing state.

Example workspace creation for one sentence repair:

```bash
uv run vntts-pregenerate create-workspace IMPORT_DIRECTORY \
  --story-index STORY_INDEX.jsonl \
  --voice-manifest VOICES/manifest.json \
  --narrator-character Centurion \
  --backend moss-tts --model MODEL_DIRECTORY \
  --generation-profile stable \
  --sentence-segment-failed QUEUE_ID
```

The read-only 2026-08-18 plan for the Character Story snapshot reconciles all
211 failures into 58 conservative sentence-boundary segmentation candidates,
26 bounded seed retries, 83 exhausted primary items for an offline fallback backend and 44
reference-comparison cases. Its state SHA-256 is the same
`10ad11119a57fda8ffccf37f31e1a949ee03d4bbd1092c99d867c36be866b875` as the
source failure report. These counts are selection evidence only; no generation
state or WAV was changed.

A fallback backend chain is part of immutable workspace configuration. It must
not mutate an existing MOSS-only workspace in place. Either create a new
config-addressed workspace that can safely carry forward exact terminal
decisions or add a versioned multi-provider control contract before mixed
artifacts are generated.

## Review and terminal routing

Automated checks run for every generated artifact. Human review is mandatory
for every technical flag and for a stratified sample of clean output across
roles, text lengths, punctuation and backend/profile/reference combinations.
If a sample reveals a substantive defect, expand review to that complete
cohort. Unchanged WAV hashes retain their decisions; regeneration invalidates
the old decision.

The final hybrid route must distinguish approved generated audio from a
deliberate live fallback. Pending review and raw failure remain nonterminal.
Final publication may include an explicit terminal `live_fallback` decision
only after source/reference discovery and offline fallback generation were
exhausted for that exact line or role. Runtime routing then becomes:

1. verified original game audio when policy permits;
2. approved pregenerated audio;
3. Pocket live synthesis with the explicitly selected role voice or Centurion
   Narrator fallback; and
4. fail-closed voice preflight for an unmapped named role.

Narrator fallback selection and force-live routing are independent controls.
Selecting Centurion as the live Narrator voice must not bypass approved
Centurion pregenerated tracks; it supplies Pocket only when an approved track
is absent or ineligible. Named roles remain blocked until mapped or explicitly
included in a Narrator fallback policy. Exact `???` continues to use the
project's established Narrator rule.
