# Authoring resume workspaces

`vntts.authoring.workbench` turns a validated, immutable legacy import into a
separate mutable resume workspace. The import, its review state and its audio
are never edited. Queue/state/audio bytes are copied through staging and an
atomic no-replace rename; the preserved `import.json` remains the root of trust
for the queue and initial history.

Resume workspaces accept only structurally consistent, job-backed legacy
imports. Standalone generation snapshots remain preserved by their importer but
are not reinterpreted as jobs; changing a manifest's source-kind discriminator,
job markers or legacy-job artifact combination is rejected.

## Config-addressed identity

A workspace directory is addressed by the complete authoring configuration:

- legacy import identity and exact import-manifest SHA-256;
- selected story-index snapshot;
- selected voice-manifest snapshot and every contained reference file;
- narrator character;
- backend, model identity and generation profile.

Changing one of those inputs creates a different workspace. Recreating the
same configuration is idempotent only after every immutable byte has been
revalidated. A changed queue, import snapshot, story/voice document, reference,
fixed path or provenance claim is a hard collision. Newly supplied voices are
allowed to differ from the legacy manifest: both the legacy digest and the new
selected digest are retained, and the workspace never claims they are the same.
Selected files are copied into application data, so resume does not depend on
mutable extractor paths.

Cohort review treats generation state as authority and the approved-only
manifest as a derived projection. Terminal acceptance commits authority before
publication, so a failed manifest replacement cannot publish an uncommitted
approval. Rejection revokes the manifest entry before committing the rejected
state, so a partial failure can leave the item pending but can never leave a
rejected WAV published. Every decision is validated against its exact plan,
cohort, policy, target identities, sample and reviewed evidence before immutable
review evidence is created.

An optional `vntts.authoring.reference_selection` voice-manifest extension is
validated against every copied candidate WAV before workspace publication. See
[authoring-reference-selection.md](authoring-reference-selection.md). This
turns a manually chosen first reference into a hash-bound workspace input rather
than a mutable UI preference.

A bounded failure-repair policy is also part of this identity. It contains only
exact queue IDs authorized for safe sentence-boundary segmentation, edge-only
silence trimming, bounded same-provider seed retry or a different-backend
offline fallback. A fourth comparison-only strategy inserts an upstream MOSS
inline pause marker into a derived synthesis prompt for exactly one current
internal-silence failure. Changing an ID, strategy or pause creates a new
workspace. `generation_command()` automatically emits the identical queue-ID
selection and refuses a caller-supplied mismatch. The child revalidates that
every selected item is still a current typed failure in the matching cohort
before rendering, so a stale repair workspace cannot regenerate unrelated or
already-reviewed work.

## Carrying reviewed character outcomes into a new narrator workspace

`create_resume_workspace()` accepts the explicit pair
`carry_forward_from=SOURCE_WORKSPACE` and
`carry_forward_characters=("Rhiannon",)`. Carry-forward is part of creation,
not a mutation of either workspace: the target is assembled and validated in a
staging directory, then published with the same atomic no-replace rename as an
ordinary workspace. The exact imported seed generation state is retained under
`provenance/seed-generation-state.json`.

The source and target must share one byte-identical queue and immutable import,
and must use the same backend, model identity and generation profile. Narrator
is forbidden in the selected character set. Only terminal approved or rejected
review outcomes are eligible; pending review, failed generation and active
attempts remain untouched.

When the target uses a partial source-reference manifest, it must explicitly
include every carried full-outcome character and its exact references. Build
that manifest with `build-reference-bindings` and repeated
`--include-base-character CHARACTER` options. Review-only legacy records can be
preserved from identical seed WAVs, but a full outcome fails closed if the
target manifest omits or changes the selected character references.

Two evidence paths are supported:

- `review-only` applies an approval or rejection when the target seed contains
  the same immutable generated item and WAV, and the source differs only in its
  review fields. This preserves review of legacy items without inventing
  synthesis metadata that their old schema never recorded.
- `full-outcome` copies a newly generated WAV and its outcome only after the
  queue annotations, synthesis text and transform, provider, model, profile,
  prompt policy, synthesis-control provenance, character identity and ordered
  character-reference hashes all match. An unrelated manifest addition may
  differ, but the selected character references may not. When the manifest
  binds an exact queue ID to a source-reference variant, character cohort
  selection still uses the base queue character while synthesis identity and
  reference validation use that exact per-line override. Source and target
  queue overrides must agree; this permits reviewed outcomes from multiple
  expression-bound variants of one character without collapsing their voice
  provenance. The carry validator reconstructs the same synthesis-provenance
  identity as the child generator, including missing-voice and repair policies,
  the exact queue-override digest, voice manifest and references, local model
  artifact and narrator selection. Any real control drift still rejects the
  copy before publication.

Every carried item records the source workspace, source state/item/WAV hashes,
character and evidence mode. Approved-only manifests retain this additive
provenance. The source state and every source WAV are rehashed immediately
before target publication; any concurrent mutation aborts and removes staging.
Recreating the exact carry-forward snapshot is idempotent, while a changed
source state produces a distinct config-addressed workspace.

Example:

```python
from vntts.authoring.workbench import create_resume_workspace

result = create_resume_workspace(
    immutable_import,
    story_index=story_index,
    voice_manifest=chosen_voice_manifest,
    narrator_character="Centurion",
    backend="moss-tts",
    model=model,
    generation_profile="stable",
    carry_forward_from=reviewed_workspace,
    carry_forward_characters=("Rhiannon",),
)
```

This operation preserves review authority; it does not approve Narrator audio,
run generation or publish a final game pack.

## Carrying current failures into repair workspaces

When a repair starts from a mutable workspace rather than the immutable legacy
seed, pass that workspace with `--carry-forward-from`. The carry ledger schema
v3 binds the exact source state and failed item before the repair workspace is
published. Same-backend sentence-boundary and bounded-seed repairs must keep
the source backend, model, generation profile, missing-voice policy, queue and
copied voice controls unchanged. A sentence repair requires at least two safe
complete sentence segments and either a typed `missed_eos_audio_limit` failure
or a typed `speech_silence` failure where only the internal-pause limit is
exceeded while leading and trailing silence remain within their global limits.
This segmentation is a bounded repair; it does not weaken the speech-quality
gate. A bounded-seed repair requires a typed `missed_eos_audio_limit` failure
with one or two exact provider attempts and permits only the remaining attempts
up to a cumulative provider maximum of three. Cross-backend Pocket fallback
keeps the stricter exhausted-attempt and Pocket default requirements below.
Same-backend and cross-backend strategies cannot share one workspace.

The inline-pause comparison is MOSS-only, accepts exactly one queue ID per run,
and records the original text hash, derived prompt hash, marker count, pause
value and synthesis-control digest. It remains bounded to three cumulative
provider attempts and is an experiment boundary rather than an automatic repair
policy. Create it with
`--inline-pause-failed QUEUE_ID --inline-pause-ms 180`; the child command must
also use `--retries 0`. After the third typed internal-silence failure, both the
failure plan and specialist plan select `offline_fallback_backend`; another
inline-marker attempt is rejected before state mutation.

Both creation and every later workspace load fail closed if the carried item,
strategy, source configuration, provider-attempt count or exact queue-ID
selection changes. Sentence and bounded-seed repairs retain the source failure
record as additive `carry_forward` provenance after either success or another
typed failure, so the repaired WAV does not erase the history that authorized
it. Existing sentence-repair workspaces whose immutable imported seed already
contains a current typed failure remain supported without carry-forward.

```sh
uv run vntts-pregenerate create-workspace IMMUTABLE_IMPORT \
  --story-index STORY_INDEX.jsonl \
  --voice-manifest VOICES/manifest.json \
  --narrator-character Centurion \
  --backend moss-tts \
  --model LOCAL_MOSS_MODEL \
  --generation-profile stable \
  --carry-forward-from CURRENT_WORKSPACE \
  --sentence-segment-failed QUEUE_ID
```

For a bounded retry, replace the final option with
`--bounded-seed-failed QUEUE_ID`. A source with one provider attempt may run
with `--retries 1`, producing only seeds 1 and 2. The carry ledger stores both
the cumulative history and the exact source-provider attempt count; legacy
attempts owned by another provider do not consume current seed space.

## Moving exhausted failures to Pocket TTS

An exhausted typed MOSS failure can move to a new immutable Pocket workspace
without mutating or relabeling the MOSS history. The exact queue ID is declared
with `--offline-fallback-failed`, and `--carry-forward-from` names the source
workspace whose current failed item is copied as evidence. Creation requires
either a typed `missed_eos_audio_limit` result with at least three completed
source attempts, or a current typed `speech_silence` result whose exact
`inline_pause_marker` repair has exhausted three attempts from that same
provider. A different source backend and `pocket-tts` as the target backend are
mandatory. Arbitrary silence failures cannot enter this path. The generated
child is restricted to exactly those IDs.

```sh
uv run vntts-pregenerate create-workspace IMMUTABLE_IMPORT \
  --story-index STORY_INDEX.jsonl \
  --voice-manifest VOICES/manifest.json \
  --narrator-character Centurion \
  --backend pocket-tts \
  --model pocket-tts \
  --generation-profile default \
  --carry-forward-from MOSS_WORKSPACE \
  --offline-fallback-failed QUEUE_ID
```

The carry ledger binds the source workspace/state/item hashes, source
provider/model/profile, failure kind, repair strategy, total and provider-local
attempts, seed, effective character and all source reference hashes. The target
workspace independently binds its Pocket model, profile, manifest and copied
reference controls. A missing or incompatible selected voice/reference blocks
readiness before backend construction and leaves the immutable MOSS failure for
manual resolution. `attempts` remains the cumulative history across providers,
while `attempts_by_provider` gives each backend its own attempt sequence.
Pocket does not expose deterministic seeded generation, so the
fallback sends `seed=None` to the backend and records `seed_applied=false`.
The integer `seed` in state remains a monotonic provider-attempt identity; it
must not be represented as an applied sampling seed. One config-addressed
fallback run permits exactly one Pocket attempt (`--retries 0`). State and
approved-manifest records retain both the fallback repair ledger and provider
counters. Pocket output passes the same typed-completion,
PCM16 mono, duration, peak, silence, checksum and manual-review gates as any
other generated artifact.

## Merging reviewed repair outcomes

Sentence and Pocket repairs remain separate config-addressed histories while
they are generated and reviewed. After review, create one successor with
`merge-workspace-outcomes`; never copy their state or WAVs by hand:

```sh
uv run vntts-pregenerate merge-workspace-outcomes PRIMARY_WORKSPACE \
  --source-workspace SENTENCE_REPAIR_WORKSPACE \
  --source-workspace POCKET_REPAIR_WORKSPACE
```

The primary workspace supplies the complete base state, including its existing
approvals and rejections. Every source must share its exact immutable import and
byte-identical queue and must contain a supported schema-v1 through schema-v4
exact-ID failure-repair policy.
Only selected repair items whose current state is `approved/approved` or
`generated/rejected` are eligible. Pending-review and failed repairs are not
terminal, are not copied and cannot satisfy a source that has no reviewed
outcome.

Each copied item must still bind the exact failed base item that authorized its
repair. A stale base item, conflicting source for one queue ID, existing base
review decision, changed state, changed WAV, active attempt, lease, partial WAV,
path collision or symlink aborts before publication. The successor is assembled
under staging and published with atomic no-replace rename. Its workspace
identity binds the base state SHA-256, every source workspace/config/state
digest and every terminal source item/WAV digest. Each merged state item and
the approved-only manifest retain that additive ledger. Repeating the exact
merge is idempotent and none of the source workspaces is mutated.

When source/reference discovery and the bounded offline fallback are both
exhausted, authoring can record an explicit terminal Pocket live-fallback
decision for one exact queue identity. This does not create or approve a WAV:

```sh
uv run vntts-pregenerate live-fallback \
  --state WORKSPACE/generated-audio/generation-state.json \
  --queue WORKSPACE/queue.jsonl \
  --reason offline_fallback_exhausted \
  --model pocket-tts \
  QUEUE_ID
```

The other accepted reasons are `reference_unavailable_after_audit` for an
absent item or an exact typed reference-unavailable failure, and
`generated_audio_rejected` for a generated WAV already reviewed as rejected.
The command requires the exact Pocket `pocket-tts`/`default` model/profile,
binds the queue line/text/speaker and prior result hash, and rebuilds the
approved-only manifest under the generation lease. Raw failed or pending-review
items remain nonterminal until this explicit decision is recorded.

Optional `--carry-forward-character` values may preserve terminal approved or
rejected decisions for unchanged non-Narrator character references in the new
workspace. Their original provider and full synthesis provenance remain on the
item; they are never rewritten as Pocket output. Pending review is not promoted
and no decision is made automatically.

After the manual narrator decision, existing seed narration must be regenerated
explicitly; an ordinary resume correctly skips valid existing WAVs. The bulk
CLI therefore accepts `--regenerate-existing` only together with at least one
explicit `--character` or `--queue-id` scope. For the narrator transition, use
`--character Narrator --regenerate-existing` against the newly created
config-addressed workspace. Before the first render, the executor scans the
whole selected scope and refuses to overwrite any approved or rejected item.
An existing pending-review WAV is replaced only after a complete typed render;
a limited render becomes an authoritative failure without publishing a new WAV,
and failed or absent selected narration follows the normal retry path. The original
import and the preserved seed state remain available as immutable evidence.
Programmatic callers of `generation_command()` must supply exact queue IDs when
requesting the same mode, preventing an empty-selection/all-items ambiguity.

The completed Rhiannon workspace was exercised read-only against a temporary
Paper Heron target on 2026-08-17. The operation carried exactly 15 terminal
Rhiannon decisions: 10 approved and 5 rejected. Fourteen matched the immutable
imported generation seed (`review-only`); the one later successful retry matched
all current synthesis controls and was copied as `full-outcome`. No Narrator
item was carried, the derived manifest contained exactly the 10 approved entries,
and the source workspace tree digest remained
`738258bd3e9f1733e8d9115f916b5c119ba41a78f6bb817696fb2e985240418c` before
and after the operation. This was an integrity acceptance only; Paper Heron was
not selected as Narrator and the temporary target was not used for generation.

The final manual narrator decision on 2026-08-17 selected **Centurion**. A final
Centurion-configured workspace must carry forward the same reviewed Rhiannon
outcomes, then regenerate only the explicit Narrator scope before any new
review or game-pack publication. The earlier Paper Heron workspace remains
historical integrity evidence and must not be reused as the final narrator
workspace.

The first bounded Centurion rollout on 2026-08-18 selected eight exact Narrator
queue IDs with `retries=0`, base seed 0 and `--regenerate-existing`. Four typed
renders completed and passed manual listening review across 4, 7, 10 and 43-word
lines. Their approved WAV SHA-256 values are, in ascending line order:

- `129fdb220b072d0399f6ec7070bc9f830ea8fdae4e346673eddff801368a17b9`;
- `4416a880a758cd364f9e913fad5e39fd960a4906fda40a796b410afc5e77a835`;
- `9020984cda82bd49bfa45d990806e2bb196286418f01662b7942293bf0f69b21`;
- `2a55cccc32dbb8679339ca3962ed46cb909ff06e0e14f0ecd5d9b554be00c399`.

The other four selected renders failed closed: three reached their bounded audio
limit before EOS and one failed the internal-silence quality gate. None
published a new WAV. After the four compare-and-swap approvals, exactly those
four authoritative items changed, all other 334 state items were unchanged,
all 15 existing Rhiannon decisions remained unchanged, and the approved-only
manifest increased from 10 to 14 exact entries. The idle state and manifest
SHA-256 values are respectively
`af758d292e411d6222804a5669076537463c52e157510b332f666a1b2c0797fd`
and
`2acd86bec929de2bb5e6dcad1282bb9cca8c07969b861e83d503dbfb4a05fe8b`.
This pilot proves scoped Centurion regeneration and review, not completion of
the remaining Narrator queue or final-pack publication.

The subsequent bounded rollout replaced all 175 remaining legacy
generated/pending-review Narrator seeds in eight explicit-ID batches. It
published 114 complete Centurion WAVs and preserved 61 bounded or quality-gated
failures without a WAV. A separate first-attempt batch covered all 10 Narrator
items that previously had no state: one completed and nine failed closed. No
legacy Narrator seed or absent Narrator item remains.

One diagnostic batch then retried the 24 shortest legacy failures whose stored
outcomes predated the current synthesis-control provenance. Only three
completed; 21 failed closed again. That low yield is the stop gate for blind
mass retries: the other 99 legacy failures remain preserved until a specific
model, text or quality-gate diagnosis justifies another attempt. The final
Narrator state is 4 approved, 118 generated/pending-review and 195 failed; 96
of those failures have already been exercised under the current Centurion
controls.

Across the rollout, all 19 pre-existing approved/rejected decisions remained
byte-identical. The final state is idle with no lease or partial WAV; its
SHA-256 is
`e5ece47ba13d7b68b44cb9628cb180dcac8ddb516ed58d481d49770f4f6194ba`.
The approved-only manifest still contains exactly 14 entries and has SHA-256
`3370b864105d85c431227f5cc283e7b95890687d4fd30c70148367c254832af1`.
No rollout result beyond the four pilot WAVs was approved automatically.

A read-only technical audit of the 118 pending Centurion WAVs verifies complete
stored duration, peak and speech-silence metrics for every item and finds no
duplicate audio digest. Twenty-seven items are prioritized for listening by
conservative attention heuristics: 14 fast-pace, 11 near-clipping, two
slow-pace, two notable-pause and one notable-silence flags, with overlaps. These
flags are review ordering aids, not approval or rejection decisions. The 195
failed Narrator items separate into 161 bounded missed-EOS/audio-limit outcomes
and 34 speech-silence failures; even the limit cohort contains short two- to
five-word lines, so the evidence does not justify a global cap increase.

## Truthful inspection and focused retry

`inspect_workspace()` projects the exact queue and authoritative state into
pending, generated/pending-review, approved, rejected and failed outcomes. It
separates sound effects, recoverable source-audio, manual-review, resolve-audio
and other skipped actions. The exact active attempt and latest outcome are
available without changing or archiving stale data.

Runtime status distinguishes this process, another live process, an
interrupted owner, review work, failures, ready work and blocked configuration.
PID start identity is fail-closed: a live PID whose start time cannot be
inspected remains externally owned; only a proven-dead PID or a proven start
identity mismatch is stale.

`inspect_generation_readiness()` evaluates either the ordinary pending/failed
set or exact selected queue IDs. `generation_command(queue_ids=...)` uses that
same selection, so an unrelated missing voice cannot block a safe focused
retry, while a selected failed line with a missing reference cannot silently do
zero work. Recoverable source audio is not opted into by the workbench until an
explicit matching preflight policy is added.

A workspace may snapshot a valid partial voice manifest. Global readiness still
reports every uncovered spoken queue item and an unfiltered command remains
blocked. A focused collection or retry may proceed only when its exact queue-ID
selection is fully covered. This permits adding references incrementally without
weakening control hashes, queue identity or final-pack provenance requirements.

An author may instead create a distinct config-addressed workspace with a
versioned missing-voice policy. The default policy is `block`. The two explicit
alternatives authorize either an exact role list or every still-unresolved
named role to use the workspace Narrator. For example:

```python
from vntts.authoring import MissingVoicePolicy, NARRATOR_ROLES
from vntts.authoring.workbench import create_resume_workspace

workspace = create_resume_workspace(
    imported_history,
    workspaces_root,
    story_index=story_index,
    voice_manifest=voice_manifest,
    narrator_character="Centurion",
    backend="moss-tts",
    model=model,
    generation_profile="stable",
    missing_voice_policy=MissingVoicePolicy(
        NARRATOR_ROLES,
        ("Poacher I", "Poacher II", "Glyndŵr"),
    ),
)
```

The generated child command carries each exact role as
`--narrator-fallback-role`; `--narrator-fallback-all` is the deliberately broad
alternative. A fallback is used only when that requested role has no usable
manifest reference and the selected Narrator reference is present. The policy
is part of the workspace identity and synthesis-control digest, so changing it
creates a different workspace. State and approved manifest records retain the
requested role, effective `Narrator`, selected narrator character and complete
policy document. The review table's `Speaker` column remains the story role and
its `Character` column shows the effective synthesis role; the header names the
selected Narrator character. Canonical audio contains only the original story
text and never prepends a speaker announcement.

The same configuration is available without a Python snippet:

```sh
uv run vntts-pregenerate create-workspace IMPORT_DIRECTORY \
  --story-index STORY_INDEX.jsonl \
  --voice-manifest VOICES/manifest.json \
  --narrator-character Centurion \
  --backend moss-tts \
  --model MODEL_DIRECTORY \
  --generation-profile stable \
  --narrator-fallback-role 'Poacher I' \
  --narrator-fallback-role 'Poacher II' \
  --narrator-fallback-role 'Glyndŵr'
```

The command prints the canonical workspace directory and whether it was newly
created. Repeating the exact command is idempotent; changing policy or another
bound input produces a different workspace instead of reconfiguring one in
place.

## Child-process trust boundary

The generated command passes `--workspace` to a fresh
`vntts.authoring.cli` process. The child independently reloads the canonical
workspace and verifies queue/output paths, run configuration, narrator, voice
manifest and every reference hash before backend construction. It passes those
expected hashes and the output directory's device/inode identity into bulk
generation, which rechecks them after backend construction, around rendering
publication and before manifest publication. Queue/output symlinks, directory
swaps and control changes fail before generated work can escape or be
misattributed.

Bulk state stores the narrator choice as a role-bound synthesis control in
addition to the full manifest/reference inventory. Final game-pack publication
may use a deliberate replacement voice snapshot only when every terminal state
item proves that exact selected manifest and references. The pack retains both
the original queue voice-manifest SHA-256 and selected SHA-256, plus the
narrator selection when present.

## Graphical workbench

Run `vntts-authoring-workbench WORKSPACE` to open the Qt shell over the same
validated boundary. It presents the authoritative pending, generated,
approved, rejected, failed, missing-reference, recoverable-source-audio,
manual-review, resolve-audio and skipped categories. The current attempt shows
its line, voice, phase, attempt, latest error and a derived live elapsed timer.
Runtime text distinguishes this child, an external owner, interruption,
attention, review, readiness and completion without relying on color.

The collection pane exposes checkboxes in the story document's declared order.
`inspect_collection_selection()` maps selected records by exact
`(line_id, text_sha256)` identity into queue-order IDs. Those IDs drive the
displayed selection counts, selection-aware readiness and child command; an
explicit empty selection never becomes the unfiltered `None` sentinel. The
selection is stored only in `QSettings` under the content-addressed workspace
ID, not in immutable workspace provenance.

Generated-audio review has a separate scope from generation. Collection
checkboxes constrain only Generate and Retry; clearing them never hides audio
that still needs a decision. Review opens on `Awaiting review` and can be
filtered independently by synthesis character, review status, source
collection and case-insensitive line text. `Narrator only` and `Characters
only` remain explicit source-scope shortcuts; named characters use the general
character filter instead of one-off buttons. `Technical attention` shows only awaiting
review items with conservative pace, peak or silence flags. Every generated
row displays duration, words per minute, peak and its attention flags; the
metrics never substitute for listening. Failed rows display a normalized
`audio limit / missed EOS`, `speech silence` or other cohort and can be filtered
by the first two actionable classes without parsing volatile backend messages.
Filtered and total counts remain visible, and a zero-row filter is never
interpreted as an unfiltered request. The exact selected queue ID is retained
across authoritative refreshes whenever it still appears in the active filter.

The review table and current line occupy the primary vertical pane with a
320-pixel usable minimum. Generation scope, readiness, voice-reference preview
and the initially collapsed technical log live in one vertically scrollable
inspector, so expanding a section grows scrollable content rather than crushing
the review table or leaving an empty group-box frame. Each section uses a
keyboard-focusable disclosure button with a right/down chevron; opening it
scrolls its first control into view. Section expansion, splitter sizes and
review filters are workspace-local settings. Reset layout remains outside the
collapsible sections, restores the review-first splitter, expands Generation
scope, collapses the other details and returns the inspector to the top.
Offscreen Qt regressions cover the default layout, a 1,440 by 900 resize and a
persisted all-expanded layout, proving every expanded control remains visible
or vertically scroll-reachable. The first pending row is selected
automatically. Previous/Next pending wrap inside the
active filter; `Ctrl+Shift+Left`, `Ctrl+Shift+Right`, `Ctrl+R`, both
`Ctrl+Return` and keypad `Ctrl+Enter`, and `Ctrl+Backspace` provide navigation,
replay, approval and rejection. Review cells are read-only so a decision
shortcut cannot open a cell editor. A
successful decision advances only after the existing state/lease transaction
has durably returned; failed validation leaves the current queue identity in
place.

Approve and Reject run on one ordered background worker, so state validation,
lease acquisition, approved-manifest derivation and file checks never block Qt
painting or input delivery. Every visible review row carries a compare-and-swap
snapshot of the exact queue digest, state digest, state-item digest and WAV
digest that was displayed. A decision revalidates only that exact snapshot;
the previously validated state document supplies unchanged manifest entries,
so unrelated generated WAVs are not reopened for every click. The worker
acquires the generation lease, prepares both replacement documents under unique
temporary names, then revalidates the full snapshot and complete lease document
again immediately before the canonical state is replaced. Lease ownership is
checked once more before the manifest replace. A changed WAV, state, queue or
lease during preparation therefore rejects the stale click without changing
either authority file. If ownership changes after the state replace, the error
states that the decision was saved while the older fail-closed manifest still
needs recovery.

While a decision is active the review controls say `Saving review`, reject a
second decision and defer window close until the worker has returned. Window
close is likewise deferred during an authority projection. Only a successful
worker result updates the affected row, counts and the shared state digest of
the remaining in-memory rows. Nonterminal decisions advance directly to the
next pending row without launching another full projection. A terminal decision
still requests a projection to recompute overall workspace state. Every
authority projection
runs off the Qt thread; the Qt callback only applies the already validated
immutable projection. A user collection change increments a scope version, so
an older worker result is discarded and reloaded instead of reverting the new
selection. A transient worker or projection failure clears stale controls but
leaves `Retry workspace load` available for an explicit in-dialog recovery
without requiring a file timestamp change.

Read-only acceptance against the real 592-line workspace on 2026-08-17 returned
the dialog constructor in 0.139 seconds while the 16.331-second full integrity
projection ran in the background. Qt delivered 469 25-ms heartbeat callbacks
with a maximum observed 0.300-second gap. The resulting default view contained
the same 196 awaiting-review rows from 338 review outcomes.

### Checksum-bound cohort review planning

`vntts-pregenerate cohort-review-plan WORKSPACE` builds a read-only versioned
review plan from the exact workspace configuration, queue, authoritative state
and generated WAV hashes. It groups only pending-review items with identical
effective voice, provider/model/profile, synthesis provenance, prompt,
reference binding, repair strategy and seed. Every technical-attention item is
sampled. Clean items are sampled deterministically from short (up to 6 words),
medium (7-15) and long (16 or more) buckets; increasing
`--clean-samples-per-bucket` produces a new plan identity.

The plan contains every covered queue ID, line/text identity, WAV SHA-256,
technical flags and sample membership. A state or seed change creates a new
plan/cohort identity. Historical pending items without complete synthesis
provenance are reported under `blocked_items`; the planner never guesses their
profile or merges them into a current cohort. Terminal approvals and rejections
are excluded rather than reviewed again.

The first read-only run against the current Character Story workspace produced
seven exact cohorts covering 17 bindable pending items and a 13-item sample;
161 older pending items were explicitly blocked because their legacy outcomes
lack complete control provenance. The source state SHA-256 remained
`b55581341e07fa701ce5a839a251c78d45f1e0b6f361b69fdebff10e28480b03`.
The planning document alone is not review authority; an immutable decision
document records that authority before atomic projection.

Publish a plan with `--output PLAN.json`, then use
`vntts-pregenerate cohort-review-decision PLAN.json COHORT_ID DECISION` with one
`--reviewed-queue-id` for every WAV actually heard. An `accepted` decision
requires every sampled WAV; `rejected` requires at least one exact sampled WAV
as evidence; `expand` requires the complete current sample and a larger bounded
`--next-clean-samples-per-bucket`. The no-replace decision document binds the
plan/cohort identity, every reviewed sample and every target line/text/WAV hash.
It records human evidence only: it does not change generation state, approvals,
the derived manifest or any real workspace. After inspecting that immutable
decision, `vntts-pregenerate cohort-review-apply WORKSPACE PLAN.json
DECISION.json` recomputes the current plan, rejects any changed state, queue,
item or WAV authority, and commits every target item in one leased state
transaction. Approved-manifest projection retains the decision, sample and
target-audio provenance. `expand` decisions cannot be applied.

The workbench exposes the same flow under `Checksum-bound cohort review`.
Planning and projection run off the Qt thread. A visible ordered table shows
every exact sample's playback state, line, source-speaker to effective-voice
mapping, duration and text. Selecting or navigating never starts playback.
`Previous sample`, `Replay selected sample`, `Stop sample` and `Next sample`
stay in one fixed row; their keyboard equivalents are `Ctrl+Alt+Left`,
`Ctrl+Alt+R`, `Ctrl+Alt+S` and `Ctrl+Alt+Right`. Replay is available both before
and after a sample has been heard and does not erase earlier listening evidence.
Only an actual `EndOfMedia` event for the captured WAV bytes counts as heard;
Stop, playback error, selection change and stale hashes do not. `Accept cohort`
remains disabled until every sample finishes, `Reject cohort` requires at least
one finished sample, and `Expand sample` requires the complete current sample.
Finish, Stop and media-error paths immediately restore replay/navigation and
the currently valid decision controls. Media errors remain visible in the
status text and can be retried in place; an integrity failure instead fails
closed and exposes `Retry workspace load`.
The progress label and action tooltips explain the heard count, selected state,
disabled gate and exact number of cohort WAVs affected. After a complete
playback, `Mark sample bad` records a reversible sample-level assessment; it
does not reject the cohort. A bad marker blocks `Accept cohort` until cleared,
while `Reject cohort` remains an explicit separate action. The confirmation
reports the heard-sample count, bad-sample count and exact target-WAV count.
New decisions bind the ordered per-sample `acceptable` or `bad` assessments to
the reviewed queue IDs; historical version-1 decisions without that additive
field remain readable. The workbench writes idempotent immutable plan/decision
evidence under the workspace
`cohort-reviews/` directory before a terminal projection, asks for confirmation,
and reloads authority afterwards. The controls never auto-apply anything on
workbench open. No command or UI decision was applied to the real Character
Story workspace during implementation or verification.

When pending outcomes live in several immutable repair workspaces, use
`vntts-pregenerate cohort-review-bundle --workspace WORKSPACE ...` instead of
opening an unrelated workbench window for every source. The versioned bundle
embeds every complete source plan, canonical source path, state/plan identity,
cohort and sampled line/text/WAV checksum. Its flattened sample inventory adds
the operator-facing reason each WAV is mandatory: either all of its technical
flags or the deterministic clean length bucket. Duplicate source paths or
workspace identities fail closed.

`cohort-review-bundle-apply BUNDLE WORKSPACE_ID COHORT_ID DECISION` revalidates
the entire bundle before constructing the ordinary source-plan decision. The
transaction still writes evidence and projects only into the selected source
workspace; it never promotes a matching-looking item in another repair source.
The returned next-bundle identity reflects the changed source state. This CLI
is the shared authority boundary for the unified Qt bundle reviewer; publishing
a bundle alone remains read-only.

Open the published inventory with `vntts-review-bundle BUNDLE.json`. Loading,
WAV preparation and decision commits stay off the Qt thread. The dialog keeps
Previous/Replay/Stop/Next in one fixed row and leaves playback/navigation live
while a source-local decision is saving. A sample counts as heard only after
exact buffered bytes reach `EndOfMedia`; replay remains available afterwards.
`Mark sample bad` is reversible, `Accept cohort` requires every current sample
and no bad marker, `Reject cohort` requires heard evidence, and `Need another
sample` is enabled only when the exact cohort has an unsampled clean item.

The real ten-source specialist bundle opened its 81 exact samples in 0.078
seconds through the targeted loader; an offscreen dialog populated 18 cohorts
and its first sample table in 0.399 seconds. Accept/Reject no longer rebuild the
entire source plan: the plan's exact state SHA binds all outcomes, while the
commit path independently rechecks workspace configuration, queue/state,
target item/WAV and lease authority. Expansion still rebuilds the selected
source plan because its sample policy intentionally changes.

A read-only acceptance on 2026-08-20 against the current 592-line Character
Story workspace first projected 141 awaiting-review rows and one remaining
checksum-bound Centurion cohort. Its one exact sample appeared as
`Narrator -> Centurion`, selected but not heard, with Replay enabled. The
subsequent human pass replayed and marked that sample acceptable, accepted the
one-item cohort and durably projected it as approved. A fresh plan then reported
zero reviewable cohorts and zero pending cohort items; 140 older generated
items remain separately blocked from cohort planning because they do not carry
the required current synthesis provenance. This closes the real-workspace
cohort acceptance gate without treating those historical items as reviewed.

The default workbench layout is review-first. Generation, readiness, voice
reference and technical sections remain reachable in the lower inspector, but
generation starts collapsed for layout version 2 and `Reset layout` restores
that state. The top summary is split into compact Review, Coverage and Selection
rows instead of one unbroken status sentence. Review tables name their identity
columns `Source speaker` and `Effective voice`; narrator rows retain the source
identity `Narrator` while showing the configured synthesis choice, for example
`Narrator` beside `Centurion`. The selected-line description uses the same
terms, so the UI does not present `Narrator` as if it were the chosen voice.
The review table keeps status, attempt count and technical quality summary next
to those identities. `Narrator only` and `Characters only` are explicit,
mutually coherent filter actions; the existing character and collection filters
remain independent. Opening a cohort sample scopes the main review table by its
exact queue ID, so unrelated Narrator outcomes do not appear beside the sample
being judged. Line and queue IDs are searchable as well as dialogue text.

The UI task boundary is explicit: workspace load and recovery stay in the top
status area; individual and cohort review share the primary panel; collection
selection and generation live in `Generation scope and controls`; selected
readiness provenance lives in `Readiness details`; source audition lives in
`Voice references`; and child output/copyable logs live in `Technical details`.
All four secondary areas are named disclosure panels, and only the primary
review task is expanded in a fresh layout.

Read-only visual acceptance on 2026-08-20 covered the real workspace at the
900x640 minimum and at 1600x1000. At the minimum size, navigation/audio controls
occupy a stable first row and Approve/Reject/Retry occupy a stable second row;
every label fits without horizontal movement during playback. The wide review
table retains an explicit scrollbar rather than shrinking identity columns into
ambiguity. At desktop size, the expanded cohort table shows fixed Playback,
Assessment, Line, Speaker-to-voice and Duration columns plus stretchable text.
Collapsed technical details consume no log-sized empty area. Geometry, splitter,
review filters and disclosure states remain persisted, while layout-version 2
migrates an older default to the collapsed generation panel.

The compact summary keeps the decision-critical Review, Coverage and Selection
counts visible. `Outcome details` is the collapsed drill-down for recoverable
source audio, manual review, unresolved source audio, skipped sound effects,
other skipped actions and the latest authoritative line/status/timestamp. This
keeps the default screen short without hiding the less common outcome classes
or folding them into a misleading generic failure count.

Accessibility does not depend on color. Runtime, review-gate and cohort-gate
states are persistent text; visible changes emit native Qt screen-reader
announcement events (assertive for runtime failures, polite for review/cohort
progress). Buttons and tables have explicit names/descriptions, the tab order
follows review filters -> table -> navigation/audio -> decisions -> cohort ->
secondary panels, and keyboard-only tests cover individual and cohort replay,
navigation, approval, rejection and recovery. Playback finish/error and async
reload tests prove those controls remain reachable after state transitions.

Voice references are searchable and navigable, and playback revalidates the
contained snapshot at click time. Recent preview choices store only validated
`(character, reference index)` values for that workspace; unknown characters,
out-of-range indexes and malformed settings are discarded. Choosing
a recent preview never changes the workspace narrator or synthesis config.
Review playback preparation runs on a background worker. It separately
revalidates the exact state-bound generated WAV, reads it through one file
handle and verifies the displayed digest again. The Qt callback gives the media
player a held read-only in-memory buffer, never a mutable pathname. Completing,
stopping or failing playback releases that buffer and immediately recomputes
the selected row actions; Approve and Reject do not require a workspace reload.
`Previous pending` and `Next pending` occupy one fixed pair of layout slots and
retain their left-to-right positions while replay is prepared, played and
stopped. Approval and
rejection use the same selected-row authority and participate in the generation
lease. On the real 592-line workspace, selected playback preparation took
1.3-2.0 ms and an approval against an isolated copy of the real state took
24.0 ms; the prior full review projection took 5.406 seconds. A state, control
or path integrity failure
stops playback, clears stale rows and disables every generation-start/retry,
review, open-folder and preview action. Stop Generation remains available for a
child already owned by this window.

Deterministic 592-row Qt acceptance also blocks the injected authority worker
for both Approve and Reject while a 5-ms heartbeat remains active. Each click
returns to the event loop in under 100 ms, disables conflicting decisions while
the durable save is active, and applies the returned authoritative result
without a synchronous full projection. Cold workspace projection and exact-WAV
playback preparation have equivalent heartbeat gates.

The deterministic UI matrix additionally covers repeat playback before and
after the heard gate, stable previous/next positions and keyboard shortcuts,
disabled-action reasons, media-error retry, stale queue/state/WAV/lease
authority, rapid sequential decisions, background-projection replacement and
close deferral during every authoritative operation. These UX paths retain the
same fail-closed integrity checks as the command-line workflow.

Generation runs through `QProcess` with program and arguments kept separate.
The dialog polls authoritative state while the child is live, preserves
ordered merged output with an incremental UTF-8 decoder, and exposes it under
checkable technical details with copyable diagnostics. Stop first requests
termination, then a per-launch tokenized timer may kill only that same child;
user stop, forced stop, failed start, I/O error and exit status remain visible
across later state polls. Geometry, splitter sizes and technical-detail state
are stored with `QSettings`, and the controls have an explicit keyboard focus
chain and accessible names/descriptions.

Checkable readiness details retain the selected collection IDs, exact ready-ID
count, contained story/voice snapshot paths and short hashes. The immutable
history display orders validated source creation/update, import and workspace
creation times chronologically and formats every value as numeric UTC,
independent of locale. New imports preserve source job times; older import
manifests omit them cleanly, so the UI says that source time is unavailable
instead of guessing from file timestamps. Every workspace records its own
timezone-aware `created_at` in the validated core document.

## Controlled real retry evidence

Clean commit `c77b87c` was used for one real, selection-scoped retry in workspace
`resume-395a5e5eec0327a3a793b66d-4b727b4671cb0ba2`. The child command contained
only queue ID `reverse1999:314605:15:d65edc619e8d32c4`, used `retries=0` and
base seed `0`, and skipped the other 591 queue records. The authoritative
failure record advanced from attempt 3/seed 2 to attempt 4/seed 3.

The typed MOSS render completed as `limited`, so the executor retained a failed
record and did not publish a WAV. Post-run verification established all of the
following:

- the other 337 authoritative state records were byte-for-byte unchanged;
- the workspace WAV inventory remained exactly the original 197 files, with no
  added, missing or partial WAV;
- the approved-only manifest remained empty;
- the immutable workspace, import and source queue hashes still agreed;
- all 201 recorded import/source artifacts and both external input hashes still
  matched their immutable inventory; and
- `active` was cleared and the generation lease was removed.

This proves safe selected resume, cumulative attempt/seed continuation and
limited-result non-publication on the real imported history. It is not evidence
of successful synthesis: the selected line remains failed and no review or
final-pack decision was made.

## Verified Patch resume and bounded missed-EOS follow-up

Commit `81073bf` corrected two issues exposed by controlled real resumes. MOSS
keeps the strict three-second hesitation guard, gives longer natural sentences
a bounded 90-wpm cadence reserve, and retains the existing 20-second absolute
ceiling. Typed LIMITED failures now retain sample/chunk/token/audio-limit
diagnostics. Authoring also downmixes typed frames-by-one/two-channel PCM before
the mono writer instead of flattening channels into time. The focused 54-test
set and the full 790-test suite passed; an independent review found no P1/P2
issues and repeated the focused, formatting and adversarial shape checks.

The first selected Patch attempt exposed the channel bug: queue ID
`reverse1999:101335:98:07f14a785de5037a` produced a mono-declared 384,000-frame
WAV whose even/odd lanes were near-identical stereo channels. That invalid
8.0-second workspace is preserved as
`resume-14d28505d16f4729c363c2de-e8a8270d7eef0826-invalid-stereo-fdf67d29ac81`
under `authoring/interrupted-workspaces`; it was never approved.

While the clean workspace was being recreated, a parallel source-mapping task
changed the external story index from SHA-256
`8af6bce1422b4cced8519f6d5a10e446981106717b3b8c81f362909522b81665` to
`93cc48052f4e022b1271d0932f5f16af614dcdbc1bc638f178281bea860f7376`.
The resulting unstarted workspace is separately preserved with suffix
`unstarted-source-change-93cc48052f4e`; no generation child used it. The real
retry instead copied the prior immutable story snapshot (`8af6...`) and voice
snapshot (`ce06030de942ab043f5bc88197c3890aaa05536fc9e0b490e74d116ce9d56eda`),
restoring the original `e8a8270d7eef0826` config identity.

The corrected Patch retry generated exactly one pending-review WAV on attempt
1/seed 0: 192,000 finite mono frames at 48 kHz (4.0 seconds), SHA-256
`ae357b956a8cadafa29af19e320e5e0a49529a84eaa3f438d4660ede846fc3ff`,
with no measured leading, trailing or internal silence. Its text-derived cap
was 8.5 seconds/850 tokens. The other 680 state records retained aggregate
SHA-256 `3a12107a9ab870e06e51a0a0f8c32f15b67df574764c36bbc0284e11433adbac`;
approved count remained zero and the WAV count changed only from 680 to 681.

The one permitted post-fix retry for the newer history used exact queue ID
`reverse1999:314606:68:fe4e011250eda914`. It advanced cumulative state from
attempt 4/seed 3 to attempt 5/seed 4, then reached the expanded bound exactly:
408,000 samples, 36 chunks, 8.5 seconds and 850 tokens. It therefore remains a
typed LIMITED failure with no WAV. The other 337 records retained aggregate
SHA-256 `198f8d504e402acad1431d63edd43e44e6c68d83167fc3283de23a40cc1a4db2`,
the WAV count stayed 197, approved count stayed zero, and the lease/active
attempt were cleared.

Both imported directories and both source job directories retained their
pre-run tree digests. Queue, contained story and voice snapshots remained
`1831f95d...`/`49f8b0fa...`, `8af6bce1...`, and `ce06030d...` respectively.
No review decision or final game pack was published.

The remaining newer-history resume gate was completed on pushed commit
`3c8764c` with a different exact queue item,
`reverse1999:314605:68:7ee2d4821f58d242`: Rhiannon's seven-word complete line
“Sorry, I can't tell you exactly where.” Preflight selected exactly one failed
item and reported it ready with three available Rhiannon references and no
missing voice; approved count was zero. The run used `retries=0` and base seed
zero, advancing the existing cumulative state from attempt 3/seed 2 to attempt
4/seed 3.

The result is one pending-review PCM16 mono WAV at 48 kHz: 157,440 samples,
3.28 seconds, SHA-256
`d713178a4596f5e6805df3f3acbef09584567f2d271764dd578f8c451107ccb9`.
Measured leading, trailing and internal silence and the silence ratio were all
zero. All 197 seed WAVs retained their expected hashes and the only added WAV
was `audio/rhiannon/f6f1e3ae6c1431e089b4204b.wav`. The other 337 state records
retained aggregate SHA-256
`efe09ad022e91d7b533e4a358a5ccacd6cdb30224b7df518ba946fb65882b197`.

Workspace controls and inputs, the immutable import and source job, and the
external story and voice-manifest controls retained their pre-run hashes. The
approved count and approved-only manifest entry count remained zero; active
attempt, lease, job-process record and partial-file inventory were empty. The
generated line remained pending review at that controlled-generation boundary:
the acceptance itself performed no review, approval, rejection or final-pack
publication.

Manual Rhiannon review was completed on 2026-08-17. The current workspace has
no Rhiannon outcome awaiting review: ten are approved, five rejected and sixteen
retain their explicit failed state. The controlled line above is now approved.
The authoritative state is idle and has SHA-256
`763f6a632f90b9776d9a26e9a9005730b26e09e343de0b15d68e43ddc75b01a7`;
the derived approved-only manifest contains ten entries and has SHA-256
`58cb1c9f97fe723025305d686b385ebeee58bb1623b3a764000f584f49f6ab6e`.
These decisions remain workspace authority, not a final game-pack publication.
Changing Narrator configuration must preserve them only through an explicit
per-item control-equivalence check; a new workspace may not silently discard or
reinterpret the completed Rhiannon review.
