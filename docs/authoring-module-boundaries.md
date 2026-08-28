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
The AST regression forbids importing the superseded private workbench names,
while the workbench test suite verifies that compatibility behavior is
unchanged.

## Regression gate

`tests/test_authoring_import_graph.py` parses every `vntts.authoring` module and
asserts that neither extracted record module can reach its higher layers and
that no pair in either former strongly connected component is mutually
reachable. It also enforces the canonical-hash leaf boundary described above.
Focused publication, loader, decision, workbench and final-pack tests must
accompany this graph gate whenever either record schema changes.
