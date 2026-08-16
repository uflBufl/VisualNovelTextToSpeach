# Authoring resume workspaces

`vntts.authoring.workbench` turns a validated, immutable legacy import into a
separate mutable resume workspace. The import, its review state and its audio
are never edited. Queue/state/audio bytes are copied through staging and an
atomic no-replace rename; the preserved `import.json` remains the root of trust
for the queue and initial history.

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

## Remaining graphical work

This module is the headless safety/status boundary for the Qt authoring
workbench. The graphical collection chooser, reference playback/navigation,
live elapsed timer, QProcess stop lifecycle, accessible focus order, persisted
layout and diagnostic presentation remain backlog work. A preserved or opened
workspace is not evidence that an imported history has resumed successfully;
that acceptance gate still requires a controlled real selected retry.
