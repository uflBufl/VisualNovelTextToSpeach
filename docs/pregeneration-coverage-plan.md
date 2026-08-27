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

The extractor-side audit is complete and checksum-bound: 53 candidate WAVs
across 19 portrait/bank groups, with 12 objective technical passes and two
reused-media transcript conflicts. This is deliberately not a manifest-ready
result. Aderyn spans distinct portrait/bank groups, one Mrs. Owen group is
anomalous, and Hotelier has no candidate meeting the minimum-duration gate.
Only a listening decision may promote a candidate into the VNTTS voice
manifest. Accepted candidates can cover 214 blocked lines. `Poacher I` (13
lines) and `Poacher II` (nine lines) have no exact installed same-speaker route
in the current story index. Glyndŵr's one line points only to
configured-unavailable audio. These 23 lines are the current evidence boundary
for an explicit fallback rather than an inferred character voice.

The first listening pass on 2026-08-18 selected three exact references:

- Aderyn's Father media `209566863` from `npcnoname324`, reference SHA-256
  `a967cb0a5909dc6e46fbb97565e38e3d0018435a2cf829284709630ed67947a6`;
- Dobharchú media `951691760` from `npcnoname323`, reference SHA-256
  `130c9242bcd322ba71cf64ce6125cb7fcc91dcae3cdacbfd08fc4c5f3e40bc82`;
  media `875779076` remains a valid but shorter reserve; and
- Poacher media `289048377` from `npcnoname325`, reference SHA-256
  `42c1ccbf0fb3e784d4c0414c37a9690c7fdf0d22cb244614a669beeb96548f0e`.

Their source hashes and normalized reference hashes exactly match the audition
report, and the new manifest entries cover 55 lines in the current 592-item
Character Story queue. The existing workspace remains immutable; generation
must use a new config-addressed workspace containing the updated manifest.
Aderyn is Rhiannon in childhood. The source name `Aderyn` remains authoritative
provenance rather than being rewritten to the adult display name. The reviewer
accepted media `477089679` (reference SHA-256
`49a0a42bc2cbac573ab0a0518e54edfb8c59709f76feb64f5cc41e7fd99e42b8`) as the
child-voice anchor for exact portrait `533706`. This does not authorize using
adult Rhiannon references for Aderyn or applying the child anchor to every
other age/portrait group. The `hero3146` and `npcnoname326` groups remain
variant-separated until listening establishes an age-appropriate reuse policy.
Aderyn crying-only media `369040295` and `172299031` are rejected. Mrs. Owen
media `599773947` remains uncertain: its voice sounded usable, but the speech
was not intelligible enough to approve as a cloning reference. A later
exhaustive exact-bank scan found the stronger 3.172-second media `562400954`
(decoded WAV SHA-256
`82e3125fbc195951006817ccd13d507b40c4d2311c2f17ebc7a37f2505e7e22b`),
which the user accepted as a legitimate Mrs. Owen voice on 2026-08-25. Its
separate generated-quality card was accepted on 2026-08-27. The original
candidate decision remains immutable; the accepted exact-bank plan and quality
review are separate checksum-bound authorities.

The expanded checksum-bound review completed all seven selected cluster cards.
Six were accepted: both Dobharchú groups, Poacher, Aderyn's Father, adult
Aderyn/Rhiannon media `792349907`, and child Aderyn/Rhiannon portrait `533706`
media `477089679`. The earlier review still records Mrs. Owen as `needs_sample`;
it was not rewritten after the stronger exact-bank evidence passed. Hotelier
still has no single-clip minimum-duration technical pass and its separate final
decision is `needs_sample`. The resulting v4 partial manifest binds 73 exact
queue IDs. A new immutable Centurion/MOSS workspace explicitly included the
three base Rhiannon references and carried all 15 terminal Rhiannon decisions
(ten approved, five rejected) without synthesis. Its preflight reduced the
missing-reference cohort from 184 to 164 lines. Forty-one lower-priority source
candidates remain unreviewed evidence, not implicit voice authority.

Hotelier's complete exact bank contains five short clips totaling 5.009
seconds. Because the role appears only once in this mostly unvoiced Character
Story, no alternate-version or public-recording search is planned. One
checksum-ledgered same-bank composite was evaluated and did not establish an
acceptable cloning reference. The supported policy is an exact Hotelier-only
Narrator fallback rather than inventing identity evidence.

## Multi-plan binding successor, 2026-08-27

The accepted Mrs. Owen plan was added through immutable multi-plan binding
schema version 2. Successor manifest
`character-story-20260827-v5-mrs-owen/voice-manifest.json` has SHA-256
`65f1f939690229a27085d198894cc7e334cb51f808c24c16754b128c43674c8e`.
It preserves all 73 v4 queue mappings exactly, adds 34 Mrs. Owen queue IDs, and
contains seven selected variants, nine voices and 13 contained reference files.
Its two source ledgers bind both plan and quality-review SHA-256 values. The v4
manifest remains unchanged at SHA-256
`0483ed213c4001e70b436a01dccec599f753aab35dd09326f1fa46cd9ab56a3e`.

Config-addressed workspace
`resume-395a5e5eec0327a3a793b66d-de274b0e064597e0` snapshots that successor plus
the exact Hotelier-only Narrator fallback. Before generation it reports 118
missing-reference lines, down from 164 in the prior reviewed workspace: 34 Mrs.
Owen lines are source-bound and 12 Hotelier lines have the explicit fallback.
It remains a seed/preflight workspace with zero new review decisions. The
generic outcome-merge API correctly refused to copy the 406 reviewed outcomes
because it accepts current failure-repair workspaces only. A dedicated
provenance-safe config rebase was therefore used; copying state or rewriting
synthesis provenance without that ledger remains forbidden.

The no-replace config-rebase successor is
`resume-395a5e5eec0327a3a793b66d-7593a7c03fe36bc3`. It carries exactly 406
terminal decisions: 324 approved and 82 rejected. Each item binds the exact
source workspace/state/item/WAV and the target route's contained reference
bytes. Nineteen records use different internal source/target variant labels,
but every source reference digest is an exact subset of its target route; the
first strict run rejected those labels before the byte-identity rule was added
and published nothing. The completed successor has workspace SHA-256
`0e7e97957875850548105cb24d4123a1cf6711d0a86eb96bc3821eb91ee33557`,
state SHA-256
`8cb8fcfc43feb5821e1eef27efa1496ce9f388c308d0cf477c89f35c86217100`,
418 state items, 12 retained target-owned failures, 49 pending queue lines and a
324-entry approved-only manifest. It remains globally `needs_attention` because
118 lines still lack a safe voice, while 61 exact pending/failed lines are
currently generation-ready.

No synthesis or review ran during the rebase. The source and target artifact
trees remained byte-identical at 431 files / SHA-256
`02e5039c196adb8e77a68300703395314dc37e000c720f8153d6d1e7ec2a0c2c`
and 218 files / SHA-256
`95c742ebde368b91598d59d1619463986f97f7f7a22c738913d5b49a92197383`.
All target state records outside the 406 exact terminal IDs remain byte-for-byte
equal to the target state. The successor has no active attempt, lease or partial
WAV, and an exact repeat returned `created=false`.

The first exact-ID generation pass attempted all 73 bound items with seed 0 and
no automatic retry. It produced 44 technically valid WAVs. A bounded second
seed recovered eight of 15 eligible missed-EOS failures, and the third and
final seed recovered three of the remaining seven. The final cohort has 55
generated items awaiting review and 18 failures: 12 safe sentence-boundary
repair candidates, four exhausted-primary offline-fallback candidates and two
silence failures requiring reference comparison. No result was approved by
generation, and the ten carried approved Rhiannon entries remain the complete
approved manifest.

## Explicit Poacher/Glyndŵr Narrator fallback, 2026-08-27

The remaining evidence boundary for `Poacher I`, `Poacher II` and `Glyndŵr`
was closed with the already selected Centurion Narrator rather than an inferred
character voice. Config-addressed seed workspace
`resume-395a5e5eec0327a3a793b66d-5d2ea3b8ef7216e8` retains the exact story,
voice-manifest, backend, model and profile controls and adds only those three
roles to the existing Hotelier fallback. Config-rebase successor
`resume-395a5e5eec0327a3a793b66d-9c7597652d106523` carries all 455 terminal
outcomes from the composed source: 384 approvals and 71 rejections. All 455
status/review pairs, WAV digests and bytes match, its approved-only manifest
has exactly 384 entries, and global missing-reference coverage falls from 118
to 95 lines: 93 Aderyn and two Dobharchú lines remain unresolved.

The rebase required two compatibility rules that remain fail-closed. A current
failure-reference overlay resolves its exact runtime control bytes and maps its
`source_reference_binding.source_voice_character` onto the target route; a
historical offline-fallback result may use only the validated reference digests
stored in `failure_repair.source_failure.source_voice_reference`. The selected
reference digest must be present in the target route. A chained
`retired_rejected` record revalidates its preserved source route against the
retirement ledger rather than treating its intentionally empty target route as
active. Focused regressions, idempotent synthetic publication and the full
1416-test suite cover these cases.

One exact generation pass then attempted only the 23 newly covered queue IDs at
seed 0 with no retries. Eight WAVs passed technical validation and remain
`generated/pending_review`; 15 attempts failed without publishing a WAV: 13
typed missed-EOS/audio-limit failures and two speech-silence failures. The
other 459 state records are byte-identical under canonical SHA-256
`03258cd54f7cd2ef3f7a78cf3efdf2a9424c875a25040f0cb8b06dab88324916`.
The workspace has 463 WAVs total, no active attempt, lease or partial output,
and generation did not change the 384-entry approved manifest. Immutable review
bundle `current-character-story-poacher-glyndwr-narrator-fallback-v1.json`
(bundle ID
`a484f39149abc5f4a1415791541dd70c4aea159c69b082fcaae2f2bd1b69aa07`)
binds exactly those eight pending WAVs in one cohort with seven required
samples. The operator completed and accepted the cohort on 2026-08-27. The
current reconciled bundle has ID
`3be4165e81b9ca9e5c3cbf5b0d0c1af6fbe604ae10f32c514f53e1fa098c1820`,
reports one completed cohort and zero remaining cohorts, and projected all
eight exact WAVs as approved. The state still preserves each original source
role and requested character while recording effective `Narrator`, selected
Centurion and `missing_voice_to_narrator`; acceptance does not rewrite Poacher
or Glyndŵr into Narrator source identity.

The exact post-run failure report is bound to state SHA-256
`2cd6f7c0efcc28e6029b2669c97e0ac7b180f4edb72dfd5cd2fc7956d48d7e81`.
It contains 19 failures: the four previously retained legacy Narrator failures
plus 15 new fallback attempts. The new failures split into 13 typed
missed-EOS/audio-limit cases and two speech-silence cases. The deterministic
repair classifier proposes ten bounded seed retries, three safe
sentence-boundary segmentation candidates, one reference comparison and one
inline-pause-marker comparison for the new cohort; the four legacy records
still require provenance recovery or controlled regeneration. This is
historical planning evidence only. The accepted cohort changed state authority,
so every follow-up must publish a fresh repair plan from the current state
rather than treating this pre-review classification as executable authority.

The 15 post-review fallback failures were then processed through only the
fresh classifier's supported transitions. Ten bounded-seed MOSS repairs
produced three pending WAVs and seven exhausted failures. Three safe
sentence-boundary repairs produced one pending WAV and two failures. The one
inline-marker comparison failed without a WAV. The latter three failures used
their final remaining MOSS seed and all stayed typed limited. The seven first
branch failures and those three final failures then received their single
unseeded Pocket fallback; all ten Pocket attempts produced technically valid
pending-review WAVs. Every branch ended with `active=null`, no generation
lease, no partial WAV and a zero-entry approved manifest. No output was
implicitly approved and the 392-approved composed source workspace was not
mutated.

The final immutable human task is
`current-character-story-poacher-glyndwr-repairs-v3.json`, root bundle ID
`ffeac3d23da277350be9f51878451bf23897e43040f9e6b41070d8899ceb076d`.
It contains five exact source workspaces, six cohorts, 14 pending items and 14
required samples. Intermediate four-item v1 and eleven-item v2 publications
are superseded planning artifacts and must not receive operator decisions.
One original failure remains outside that bundle:
`reverse1999:314606:62:e3f44f0529c8ced0` is an unrepairable-by-inference
speech-silence result. Its narrow immutable audit
`current-character-story-poacher-ii-reference-audit-v1` has audit ID
`2c650210df98ab58e3cb18c7dc334a64927048ab1dbc238cc39b65ea52457def`,
one case and three blinded Centurion candidates. Selecting a reference is a
separate listening authority, not approval of generated speech.

The first real-story Aderyn review refined the fixed-corpus result. Adult
Aderyn is not rejected as a whole: two natural lines were approved, while four
lines with conspicuous slow pacing or mid-phrase pauses were rejected. Those
four outputs are all below the existing 110-WPM technical-attention threshold,
so pacing remains a review flag rather than an automatic generation failure.
The child portrait variant sounded unacceptable on story text despite its
previously acceptable source clip and fixed evaluation. The immutable v3 review
and v4/v5 bindings remain preserved as the evidence that was actually used for
this run.

The child portrait variant was retired on 2026-08-27 through immutable binding
successor `character-story-20260827-v6-child-aderyn-retired`. Its manifest
SHA-256 is
`0c74667fdf8c617fb311c26f01d4f66989a9019ebc733fb56a9942c27a84d7ff`.
The version-3 ledger binds portrait `533706`, variant
`cluster-6a3c52e451a4abb5a69c32a8-anchor-1`, reference SHA-256
`49a0a42bc2cbac573ab0a0518e54edfb8c59709f76feb64f5cc41e7fd99e42b8`
and its three exact queue IDs. The reference remains copied as inactive
provenance, but the active manifest has six selected variants and 104 queue
overrides; none of the three retired IDs can select it.

Config-addressed seed workspace
`resume-395a5e5eec0327a3a793b66d-231fde5834d2ede6` retains Centurion, MOSS stable
and the exact Hotelier-only Narrator fallback. No-generation rebase successor
`resume-395a5e5eec0327a3a793b66d-e80f38bf49a43635` carries the existing 406
terminal decisions unchanged: 324 approvals and 82 rejections. Exactly the
three retired child IDs are `retired_rejected`; their target-reference lists
are empty, they are absent from the active overrides and approved-only
324-entry manifest, and approvals could not have crossed this boundary. The
successor workspace SHA-256 is
`f5dde41cccbe1a9a0b4f9d369555e0b193c4e731951e519a1929db51f0829de6` and
state SHA-256 is
`da7a45d9b9c5eda7b8ac70a8633eccec3574a1922d6c0465f10725e74caed9b7`.
There is no active attempt, lease or partial WAV. The source and seed workspace
and state hashes exactly match the snapshots recorded by the rebase ledger,
and an exact repeat returned `created=false`.

The subsequent real-story Dobharchú review found a different failure mode:
the source identity can produce good lines, but MOSS delivery is inconsistent.
At state SHA-256
`b55581341e07fa701ce5a839a251c78d45f1e0b6f361b69fdebff10e28480b03`,
the 37-line portrait variant has 11 approved, 17 rejected, four pending and
five failed items. The 11-line portrait variant has no approvals, two rejects,
three pending and six failed items. The reviewer rejected lines for
intermittently slow delivery and excessive pauses between phrases. Thirteen of
the 17 rejected outputs in the larger variant are below 110 WPM, but four are
not; pace alone therefore cannot authorize a repair. The current speech-quality
projection reports zero internal silence on these rejected artifacts, so it
also does not detect the perceptually objectionable pauses. The root cause was
an int16-versus-normalized-dBFS mismatch in analysis version 1. A read-only
version 2 projection over the same checksum-bound state flags all 19 rejects for
technical attention and identifies 18 through `slow pace` or `notable pause`;
the state remains byte-identical. One approved line is also pause-flagged, so
these metrics remain attention aids rather than automatic review decisions.
The 11 approved WAVs remain exact line authority, not proof that either
complete Dobharchú variant passes a reusable cohort gate. Leave the seven
unreviewed outputs pending and preserve the immutable v3 review/v4 binding as
historical inputs. A successor cohort must compare bounded alternative
reference/profile/backend renders before reuse in a later story.

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

## Semi-automatic reference selection

The source-reference review contract is defined in the extractor's
`docs/reference-audition-automation.md`. Automation should eliminate repetitive
listening, not guess identity. The extractor can reject non-speech, align local
ASR to the exact transcript, estimate contamination and speaker count, cluster
embeddings within exact portrait/bank groups and rank groups by coverage. A
human still approves the first anchor for every ambiguous group.

VNTTS then consumes checksum-bound decisions, keeps portrait variants separate
and synthesizes one fixed evaluation corpus per accepted anchor. Blind
source/result comparison is the second gate. Only candidates passing both the
source and generated-quality gates may enter a new config-addressed workspace;
changed WAV or decision hashes invalidate only affected controls.

Human review has two different lifetimes. Voice/reference acceptance is
reusable across stories only while the exact age/portrait variant, ordered
reference hashes and model/profile remain unchanged. Generated-WAV authority is
line-specific, but it does not require listening to every clean line: every
technical-attention item plus a deterministic short/medium/long sample can
authorize an explicit cohort decision. That decision must retain the sampled
queue IDs and WAV hashes and project terminal decisions to each covered item.
A new story samples its new generated cohort; it does not repeat source-voice
discovery or a full listen-all pass unless the sample exposes a defect. The
authoring pipeline now owns this checksum-bound cohort-decision transaction,
including reusable voice-control gates and line-specific terminal projection.
Final publication still requires every line to have terminal approved/rejected
authority; an accepted voice gate alone is never line approval.

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

Canonical generated speech contains only the story text. Automatically
prepending phrases such as "Poacher I says" would change the script, duplicate
the visible nameplate and make ordinary dialogue unnatural. Optional spoken
speaker identification is therefore a separate, disabled-by-default live route.
It announces only a changed visible speaker with the configured Narrator voice,
records independent route/outcome provenance, and stays outside canonical story
WAVs and review authority. It never creates an auto-advance action, and original
game-audio routes skip it rather than overlaying speech.

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
  provider/model and provider-specific seed semantics per item. A Pocket
  artifact must pass the same WAV, silence, checksum and manual-review gates as
  a MOSS artifact.

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

Four conservative actions now have an executable, config-addressed path. Create
a new workspace with repeated `--sentence-segment-failed QUEUE_ID` and/or
`--trim-edge-silence-failed QUEUE_ID`, or
`--bounded-seed-failed QUEUE_ID`. An exhausted exact failure instead creates a
Pocket workspace with `--carry-forward-from MOSS_WORKSPACE` and
`--offline-fallback-failed QUEUE_ID`. The generated child command carries the
same exact queue IDs. Before any render, bulk generation requires the selected ID
set to equal the repair policy, reloads authoritative state, requires a current
typed `failed` outcome and verifies that its failure kind and metrics still
match the requested strategy. Historical string-only failures remain planning
evidence and must first produce a typed current failure; they are not silently
authorized.

The offline fallback path also requires a carried source item from a different
backend and a Pocket target. It preserves the MOSS workspace byte-for-byte,
binds source state/item/reference hashes, and retains cumulative attempts while
starting Pocket's provider-local seed sequence at the requested base seed.
Terminal non-Narrator decisions may be copied explicitly with their original
provider provenance; no pending or failed item becomes approved through
carry-forward.

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

Reference comparison and selection are implemented as a separate immutable
manifest workflow, documented in
[`authoring-reference-selection.md`](authoring-reference-selection.md).
`reference-report` checksum-binds objective metrics for every candidate;
`select-reference` publishes a new no-overwrite manifest only after a human
chooses the candidate number. Workspace creation verifies the selection
extension against its copied WAV bytes. Because a reference change affects the
whole character, generation still requires an explicit queue-ID cohort and all
new WAVs remain pending review.

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

The final hybrid route distinguishes approved generated audio from a deliberate
live fallback. Pending review and raw failure remain nonterminal. The
`live-fallback` authoring command records the exact queue/text/speaker, prior
result hash, reason and fixed Pocket model/profile only after source/reference
discovery and offline fallback generation were exhausted. Final publication
ships that decision in a checksum-bound metadata ledger; runtime accepts it
only with the matching Pocket backend. Runtime routing then becomes:

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
