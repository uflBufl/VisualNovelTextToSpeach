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

## Regression gate

`tests/test_authoring_import_graph.py` parses every `vntts.authoring` module and
asserts that neither extracted record module can reach its higher layers and
that no pair in either former strongly connected component is mutually
reachable. Focused publication, loader, decision, workbench and final-pack tests
must accompany this graph gate whenever either record schema changes.
