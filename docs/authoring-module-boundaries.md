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
message while sharing one implementation.

`workbench` keeps its historical private aliases and exposes public wrappers
with `AuthoringWorkbenchError` semantics. Production callers now use those
public wrappers for path, file, JSON and digest operations; atomic directory
publication callers use `publication.rename_directory_no_replace` directly.
All read-only production consumers load complete workspaces through
`load_workspace_authority()`, which binds the validated document to the exact
workspace-file SHA-256 snapshot; `_load_workspace` is now workbench-internal
compatibility only. Callers that need only the directory and document discard
the returned digest after validation rather than reopening a weaker snapshot.
The AST regression forbids importing the superseded private workbench names,
while the workbench test suite verifies that compatibility behavior is
unchanged.

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

## Generation state foundation

`generation_state` begins the state boundary with immutable queue loading: it
reads queue bytes once, hashes that exact snapshot and parses only a temporary
copy of those bytes. Bulk generation, live-fallback, known-role reuse and final
pack publication therefore share one race-resistant public loader.

`bulk_generation._load_stable_queue` remains a compatibility alias. The
import-graph regression forbids production imports of that private name and
ensures the state foundation cannot reach the bulk orchestrator. Semantic state
document validation remains in `bulk_generation` until its complete validation
closure can move without weakening typed failure, repair, provenance or audio
event checks. Its current public `validate_generation_state_document()` facade
validates an isolated copy, and all production callers outside the orchestrator
use that facade rather than importing `_validate_state_document`. This keeps the
remaining extraction boundary explicit without exposing a mutable validator.

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
and all of its copied reference controls. Cohort planning, config rebase,
terminal-conflict publication and known-role reuse consume these leaf APIs
directly.

`workbench._workspace_config_fingerprint` remains an identity-preserving alias,
while `_selected_voice_manifest` remains a compatibility wrapper that supplies
`AuthoringWorkbenchError` as the leaf validator's error type. The AST regression
forbids production imports of both historical private names and ensures
`workspace_config` cannot reach the workbench dependency magnet. Canonical JSON
ordering, SHA-256 identity, path containment, selected-path equality and copied
reference verification are unchanged.

## Regression gate

`tests/test_authoring_import_graph.py` parses every `vntts.authoring` module and
asserts that neither extracted record module can reach its higher layers and
that no pair in either former strongly connected component is mutually
reachable. It also enforces the canonical-hash, generation-lease, generated
manifest and generation-state leaf boundaries described above.
Focused publication, loader, decision, workbench and final-pack tests must
accompany this graph gate whenever either record schema changes.
