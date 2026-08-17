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

Two evidence paths are supported:

- `review-only` applies an approval or rejection when the target seed contains
  the same immutable generated item and WAV, and the source differs only in its
  review fields. This preserves review of legacy items without inventing
  synthesis metadata that their old schema never recorded.
- `full-outcome` copies a newly generated WAV and its outcome only after the
  queue annotations, synthesis text and transform, provider, model, profile,
  prompt policy, synthesis-control provenance, character identity and ordered
  character-reference hashes all match. An unrelated manifest addition may
  differ, but the selected character references may not.

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
collection and case-insensitive line text. `Rhiannon only` and `Exclude
Narrator` are explicit shortcuts, filtered and total counts remain visible,
and a zero-row filter is never interpreted as an unfiltered request. The exact
selected queue ID is retained across authoritative refreshes whenever it still
appears in the active filter.

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
active filter; `Ctrl+Shift+Left`, `Ctrl+Shift+Right`, `Ctrl+R`, `Ctrl+Return`
and `Ctrl+Backspace` provide navigation, replay, approval and rejection. A
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
