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

Voice references are searchable and navigable, and playback revalidates the
contained snapshot at click time. Recent preview choices store only validated
`(character, reference index)` values for that workspace; unknown characters,
out-of-range indexes and malformed settings are discarded. Choosing
a recent preview never changes the workspace narrator or synthesis config.
Review playback separately revalidates the exact state-bound generated WAV
before playback; approval and rejection independently reload state and
participate in the generation lease. A state, control or path integrity failure
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
