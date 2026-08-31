# Authoring module boundaries

Authoring wire records must remain below orchestration, UI and final-publication
modules in the import graph. This keeps checksum validation reusable without
making runtime consumers import the workflows that create the artifacts.

## Source-reference quality

`source_reference_quality_records` owns the versioned quality-review schema,
immutable public result and error types, complete artifact validation, progress
projection and atomic local decision update. The existing
`source_reference_quality` module remains the public publishing and CLI API and
re-exports the same symbols. The review importer, composite publisher and Qt UI
depend on the record layer rather than on each other.

This removes the former cycle among `reference_composite`,
`source_reference_review`, `source_reference_quality` and
`source_reference_quality_ui`. Moving the definitions does not change the JSON
schema, sorted atomic writes, accepted decision vocabulary, public import paths
or public exception/result module identity.

## Failure-reference binding

`failure_reference_binding_records` owns the versioned binding schema,
immutable public binding/error records and the complete self-contained binding
loader. `failure_reference_binding` remains the publisher and re-exports the
same public symbols. `game_pack` and `workbench` consume only the validated
record layer; they do not import the audit/binding publication workflow.

This removes the former cycle among `failure_reference_audit`,
`failure_reference_binding`, `game_pack` and `workbench`. Binding identity is
still the SHA-256 of the same canonical JSON projection, reference paths remain
symlink-safe and contained, and queue override inventory validation remains
unchanged.

## Canonical document identity

`authority.canonical_document_sha256` is the leaf API for the stable SHA-256 of
canonical JSON-compatible records. Production authoring modules must not import
the historical private `bulk_generation._canonical_sha256` helper. The first
dependency-magnet extraction slice migrated all 21 production consumers,
including workbench, cohort, reference, config-rebase, terminal-conflict and
final-pack workflows, without changing any calculated document identity.

`bulk_generation._canonical_sha256` remains available for compatibility, and
the few modules whose existing tests exercised their imported private alias
retain a local alias to the leaf function. New code must use the public
authority name. The import-graph test performs an AST inventory over all
production modules and fails if a direct private bulk-generation hash import is
introduced again.

## Terminal-conflict record semantics

`terminal_conflict_records.is_terminal_review_outcome()` owns the shared
definition of a terminal reviewed state item: approved/approved or
generated/rejected. Config rebase consumes this record-level predicate directly;
workbench retains `_terminal_review_outcome` as an identity-preserving
compatibility alias. The import-graph gate forbids production use of the old
private name so the terminal status vocabulary cannot diverge between the two
workflows.

## Workspace filesystem foundation

`workspace_foundation` is a leaf module for canonical POSIX-relative paths,
contained path resolution, non-symlink regular-file reads, exact JSON-object
snapshots and full SHA-256 value validation. It has no dependency on workbench,
generation, publication or UI modules. Each primitive accepts the caller's
domain error type so existing workflows retain their exact exception class and
message while sharing one implementation. It also owns immutable workspace-tree
snapshot copying: source trees must be directories without symlinks, every file
is copied through contained-path checks, and the caller receives the source
SHA-256 ledger used for post-publication authority verification.

`workbench` keeps its historical private aliases and exposes public wrappers
with `AuthoringWorkbenchError` semantics. Production callers now use those
public wrappers for path, file, JSON and digest operations; atomic directory
publication callers use `publication.rename_directory_no_replace` directly.
All read-only production consumers load complete workspaces through
`load_workspace_authority()`, which binds the validated document to the exact
workspace-file SHA-256 snapshot; `_load_workspace` and
`_load_workspace_snapshot` are now workbench-internal compatibility only.
Callers that need only the directory and document discard the returned digest
after validation rather than reopening a weaker snapshot. Missing-voice reuse,
reuse review, voice-repair comparison and terminal-conflict publication retain
their exact workspace digest bindings through the same public API.
Terminal-conflict publication calls the foundation tree-copy API directly;
workbench retains `_copy_workspace_tree_snapshot` only as an internal
compatibility alias. The AST regression forbids importing the superseded
private workbench names, while the workbench test suite verifies that
compatibility behavior is unchanged.

Outcome reconciliation enters workbench through
`merge_reconciled_workspace_outcomes()`, which requires the caller's immutable
reconciliation selection and keeps the unrestricted outcome merge API
separate. The internal `_merge_workspace_outcomes` implementation remains a
workbench detail and is protected from renewed production imports by the AST
regression.

## Generation lease foundation

`generation_lease` is the leaf owner of the generation lease schema, process
identity checks, exclusive lease lifecycle and contained recovery of abandoned
artifacts. `publication`, workbench and the missing-voice live-fallback writer
consume its public `GenerationLease` and process helpers directly, so atomic
publication no longer imports the private lease implementation from the bulk
generation orchestrator.

`bulk_generation` re-exports `BulkGenerationError`, the lease constants and
process helpers and retains `_GenerationLease`, `_process_started_at` and
`_archive_interrupted_artifact` aliases for compatibility. All compatibility
imports resolve to the shared leaf objects, while the import-graph test forbids
any production module from importing the historical private lease name.
Lease payload fields, stale-owner PID/start-time checks, advisory guard locking,
crash archive naming and post-commit cleanup behavior remain unchanged.

## Generated manifest foundation

`generation_manifest` is the leaf owner of generated-WAV structural validation,
approved-only entry projection and atomic generated-audio manifest writing. The
live-fallback, config-rebase, terminal-conflict, workbench and final-pack paths
consume its public functions directly instead of importing private helpers from
the bulk generation orchestrator.

`bulk_generation` retains `AudioQuality`, `inspect_generated_wav`,
`_approved_manifest_entries`, `_write_generated_manifest_from_state` and
`_validate_success_file` compatibility names backed by the same leaf objects.
The projection preserves entry ordering, optional provenance fields, exact WAV
checksum/quality checks, contained POSIX paths and the existing atomic manifest
schema. The import-graph regression forbids production callers from importing
the historical private projection names.

## Speech-quality foundation

`speech_quality` owns versioned PCM16 silence measurement, pause-span
diagnosis and the immutable result/error types used by generation-state
validation. It depends on audio primitives and sentence-repair parsing, but
cannot reach bulk orchestration. Bulk generation retains the established
constants, classes and measurement functions as compatibility exports;
workbench, model benchmarking and robustness reporting consume the foundation
module directly. The exported dataclasses and validation exception retain
their historical bulk-generation pickle and introspection identity.

## Public compatibility facade

`vntts.authoring` preserves its 435-name public `__all__`, but resolves each
name lazily from a static owning-module table and caches the result. Importing
the package or a leaf such as `generation_state` no longer imports workbench or
PySide. Normal Python submodule imports remain available, and a fresh-process
regression binds the exact ordered export inventory by SHA-256 while checking
that UI modules stay unloaded.

## Generation state foundation

`generation_state` owns immutable queue loading: it
reads queue bytes once, hashes that exact snapshot and parses only a temporary
copy of those bytes. Bulk generation, live-fallback, known-role reuse and final
pack publication therefore share one race-resistant public loader.

The same foundation now owns state schemas, live-fallback wire constants and
the complete semantic validation closure for typed failures, repair records,
synthesis provenance, active attempts, generated WAV identity, speech quality,
audio-event results and terminal-conflict provenance. Validation operates on an
isolated copy and returns that validated document. Production workflows import
this API directly; bulk generation re-exports it and retains
`_load_stable_queue` plus its internal helper aliases for compatibility.
The import-graph regression proves that `generation_state` cannot reach the
bulk orchestrator and that the old private validator definition cannot return
to it.

## Bulk orchestration public boundary

Production modules consume named bulk-generation APIs for checksum-bound cohort
commits, typed PCM normalization, sentence and inline-pause repair eligibility,
and immutable generation-control snapshots. The historical
`_review_generation_cohort`, `_generated_mono_pcm`,
`_sentence_repair_matches_failure`, `_inline_pause_matches_failure` and
`_snapshot_control_files` names remain identity-preserving compatibility aliases
for tests and external callers; they are not production dependencies.

The import-graph regression inventories every production authoring import and
fails if any underscore-prefixed compatibility helper is imported from
`bulk_generation`. This ratchets the dependency magnet without changing cohort
CAS authority, render validation, repair selection or source-change detection.

## Workspace configuration foundation

`workspace_config` owns the canonical workspace configuration fingerprint and
the contained, checksum-validated path resolution for a selected voice manifest
and all of its copied reference controls. It also normalizes every supported
run-config generation and loads typed missing-voice and failure-repair policies.
Cohort planning, config rebase, terminal-conflict publication and known-role
reuse consume these APIs directly.

`workbench._workspace_config_fingerprint` remains an identity-preserving alias,
while `_selected_voice_manifest` remains a compatibility wrapper that supplies
`AuthoringWorkbenchError` as the leaf validator's error type. The AST regression
forbids production imports of both historical private names and ensures
`workspace_config` cannot reach the workbench dependency magnet. Canonical JSON
ordering, SHA-256 identity, path containment, selected-path equality and copied
reference verification are unchanged.

`workspace_voice_runtime` owns the effective runtime voice projection. It
combines the checksum-validated selected manifest with an optional validated
failure-reference overlay, constructs the effective registry and derives exact
per-queue voice overrides. The module cannot reach workbench. Config rebase and
failed-control carry consume these public projections directly, while
workbench's historical private helpers delegate with
`AuthoringWorkbenchError` semantics. The established public
`FailureReferenceRuntimeBinding` import and pickle identity remain compatible.

Optional workspace provenance layers are validated through
`validate_workspace_provenance_extensions()`. Its ordered contract covers
carry-forward, input configuration, offline fallback, outcome merge and
terminal-conflict merge without exposing the five implementation validators.
The terminal-conflict publisher uses this composite API; an AST inventory
prevents production code from importing any of the private validators again.

## Workspace generation-state boundary

`workspace_state.load_stable_workspace_generation_state()` owns immutable,
inactive generation-state capture for workflows that consume an existing
workspace. It binds the queue snapshot to `seed_inventory`, parses the exact
state payload once, validates it against that queue through the public semantic
validator, confirms the state path still has the same SHA-256 and rejects an
active attempt, generation lease or partial WAV. The exact payload and digest
are returned for downstream CAS bindings.

Missing-voice reuse and review, voice-repair comparison and terminal-conflict
publication use this domain API directly. Workbench retains
`_stable_workspace_state` only as an `AuthoringWorkbenchError` compatibility
wrapper. The AST regression forbids production imports of the private name and
ensures `workspace_state` cannot reach workbench.

## Regression gate

`tests/test_authoring_import_graph.py` parses every `vntts.authoring` module and
asserts that neither extracted record module can reach its higher layers and
that no pair in either former strongly connected component is mutually
reachable. It also enforces the canonical-hash, generation-lease, generated
manifest and generation-state leaf boundaries described above.

## Authoring CLI command families

`cli_dispatch.CommandFamily` is the small ownership contract for commands that
have been extracted from the historical `cli.py` dependency magnet. Dispatch
rejects overlapping ownership instead of silently choosing the first handler.
The top-level parser still owns common error translation and composes family
parsers in the established order, so command names, help ordering, defaults,
exit codes and JSON output remain compatible during incremental migration.

`cli_legacy` owns legacy discovery/import, standalone generation import and
blind-listening import commands. `cli_delivery` owns delivery annotation,
`cli_queue` owns queue preflight/publication, and `cli_workspace` owns core
workspace creation and immutable outcome merges. `cli_speech_robustness` owns
corpus publication/checking, diagnostic ASR and managed-ASR installation/status;
it translates its implementation errors through one family error so `cli.py`
does not import those workflow modules. `cli_audio_events` owns all nine exact
omission, projection-fallback, source-review, production-composition and
composition-workspace commands. It registers its workspace and review parsers at
their two historical composition points so top-level help order remains stable,
but exposes one immutable command inventory and one dispatch handler. The
top-level CLI imports only that family boundary while retaining shared
translation of its review/composition domain errors.

`cli_render_reviews` similarly owns the four checksum-bound render-hypothesis
review commands plus the three reference-render comparison, blind-listening and
preference-import commands. It exposes separate hypothesis and reference parser
registration functions at the two historical composition points, while one
immutable command inventory prevents split or overlapping dispatch ownership.
The family re-exports both workflow error types for top-level parser translation;
`cli.py` must not import `reference_render_comparison` or
`render_hypothesis_review` directly. A normalized parser-contract digest binds
all seven commands' help, action order, types, choices, defaults and required
flags, while focused tests bind their JSON and implementation call contracts.

`cli_cohort_reviews` owns the five checksum-bound plan, cross-workspace bundle,
decision and atomic projection commands. Its planning and decision parser
builders preserve the two historical help-order positions, and its family
handler preserves fail-fast queue-selection validation before any artifact I/O.
Plan and decision loaders remain private to the family handler. Voice-quality
consumers load their evidence inside their own family boundary, so `cli.py`
remains free of concrete `cohort_bundle` and `cohort_review` imports. A
normalized parser digest, single-owner dispatch test and focused
validation/output tests bind this contract.

`cli_silence_comparison` owns the checksum-bound comparison publication,
integrity inspection and blind-listening session commands. It also owns the
comparison-only target-silence default and re-exports the domain error, so
`cli.py` does not import either `silence_comparison` or `failure_repair` for this
workflow. Its normalized parser digest and focused tests bind all three distinct
JSON result schemas in addition to parser order and implementation calls.

`cli_terminal_conflicts` owns the five resolution, unchanged/cohort decision
carry, reconciliation-successor and immutable workspace-merge commands. It
re-exports the resolution, review and successor domain errors for top-level
parser translation and keeps all four concrete terminal-conflict workflow
modules out of `cli.py`. Its contract tests bind the contiguous parser position,
workspace-root default and the intentionally distinct document, raw-progress
and workspace-result JSON forms.

`cli_voice_quality` owns the reusable cohort-backed quality gate and bounded
voice-repair comparison, candidate-workspace and exact-command operations. It
loads cohort evidence directly instead of coupling `cli.py` to either the
concrete cohort workflow or the sibling cohort CLI family. Its parser contract
binds the default `stable,natural` comparison profiles and workspace-root
default; focused tests separately preserve document and argv JSON outputs.

Shared missing-voice and failure-repair flags live in
`cli_generation_options`, which is reused by generation and workspace families
without coupling them to each other. `cli_generation` now owns generation,
review, state publication/status and bounded repair planning. `cli_references`
owns failure-reference, missing-voice, portrait-alias and source-reference
workflows. `cli_workspace_transitions` owns immutable reviewed/fallback/config
successors, while `cli_pack` owns final publication. `cli_errors` is the one
user-facing exception boundary.

The top-level `cli.py` contains only parser composition, immutable
single-owner family registration, shared error translation and dispatch. It no
longer imports concrete workflow leaves. `cli_contract` restores the historical
95-command help order after family composition and hashes both that complete
order and the normalized parser schema: every option, positional, type, required
flag, choice, default and help text. The checked-in v1 contract prevents
extraction work from silently changing the public CLI. Its gate also requires
the parser inventory to equal the union of family inventories with exactly one
owner per command. The contract runs on every supported Python minor (3.11,
3.12 and 3.13), guarding the compatibility shim that preserves historical
argparse help order. Family modules depend only on workflow leaves and never on
`cli.py` or sibling command families. Focused tests bind their parser defaults,
ordering, single-owner dispatch and existing JSON behavior; the complete suite
remains the final compatibility gate for each extraction slice.

The audio-event, render-review, cohort-review, silence-comparison,
terminal-conflict and voice-quality slices additionally retain focused concrete
import-boundary gates.
Focused publication, loader, decision, workbench and final-pack tests must
accompany this graph gate whenever either record schema changes.
