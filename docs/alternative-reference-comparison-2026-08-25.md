# Alternative-reference render census, 2026-08-25

This document records the bounded render-only experiment for the seven exact
Character Story failures that remained after the merged-state census. It is
evidence, not a review decision: no generation state, WAV authority, review
status or approved-only manifest was changed.

## Publication contract

`failure-reference-audit --queue-id` now accepts an explicit set of current
failed queue IDs. The immutable audit still binds the workspace, queue, state,
voice manifest and every copied reference byte. A separate
`failure-reference-render-comparison` plan requires at least two arms with the
same ordered queue IDs and different exact reference controls. Cross-group
controls are allowed only when both groups parse to the same exact
`Source reference <character> cluster-*` family.

Rendering uses the workspace backend, model and profile with seed 0 and cache
policy `bypass`. Typed `cancelled` or `limited` results publish no WAV. Complete
results and every copied control are checksum-bound, and each strict model
report has its own SHA-256 in the comparison document. A blind session can be
created only for queue IDs complete in every arm. None of these commands writes
generation state or infers acceptance.

The public CLI sequence is:

```bash
uv run vntts-pregenerate failure-reference-audit WORKSPACE \
  --queue-id EXACT_FAILED_QUEUE_ID \
  --output AUDIT_DIRECTORY
uv run vntts-pregenerate failure-reference-render-comparison PLAN.json \
  --output COMPARISON_DIRECTORY
uv run vntts-pregenerate failure-reference-render-session \
  COMPARISON_DIRECTORY --output LISTENING_DIRECTORY --seed SEED
uv run vntts-pregenerate failure-reference-import-listening \
  FRESH_AUDIT_DIRECTORY COMPARISON_DIRECTORY LISTENING_DIRECTORY/session.json \
  EXACT_FAILED_QUEUE_ID
```

The operator-authored plan uses schema
`vntts.authoring-reference-render-input` version 1. Every arm lists the same
ordered `queue_id`, `case_group_id`, `candidate_group_id` and `candidate_id`
records; the loader rejects duplicate controls, changed audit identity and
cross-character candidate groups before model startup.

The final import step never trusts an opaque side label by itself. It resolves
the completed preference through the mode-0600 blind key, matches the selected
arm to one complete rendered WAV, then maps its exact reference SHA-256 into a
fresh one-case audit of the current failed state. The decision and subsequent
binding retain comparison, source-audit, listening-session, blind-key, report,
trial, render and reference hashes. This closes the provenance gap between an
old immutable comparison and a newer config-addressed failure workspace without
rewriting either source.

## Narrator results

The final checksum-hardened publication is
`current-character-story-narrator-alternative-reference-v2`, comparison ID
`51b1b0fffdae98f221d3faff066cd063498a5edbb4059ce9da8fcf29770746af`.
It compares Centurion reference 2
(`d2be6ca0eedf63e2f36e9dfdccdddad90a622a9cc5102d46aa67b6ec456c1f20`)
with reference 3
(`f14f99ac31ebd5f4124bb76ba3d8478dac82ec3a7986c7e776dae4a88cf59515`):

| Queue ID | Reference 2 | Reference 3 | Consequence |
| --- | --- | --- | --- |
| `reverse1999:314606:54:0450c81c4d1b3cc4` | complete, `5167f529013c...` | complete, `eae1ee930f18...` | one legitimate blind A/B trial |
| `reverse1999:314608:58:c3e23840e6ecc840` | complete, `7ff11a602d9a...` | typed limited | standalone evidence only |
| `reverse1999:314608:94:f6c23264391ffae3` | typed limited | complete, `e6af9b24e2cc...` | standalone evidence only |
| `reverse1999:314606:6:3511125b2e41a19f` | typed limited | typed limited | no publishable candidate |

The one matched trial is in blind session
`current-character-story-narrator-alternative-reference-v2`. A second direct
publication for `reverse1999:314606:43:09977e2b04515b66` has comparison ID
`84a920b70271c64a30372c889f0305905e068c8f20e300abb6ebb24302ce01f0`;
both alternative references ended typed limited, so it has no blind trial.

The matched blind session completed at `1/1` on 2026-08-27. The operator chose
candidate A, which the checksum-bound key resolves to
`centurion-reference-03`; its comparison WAV SHA-256 is
`eae1ee930f182b6262be5272519dd2c3170c2efc59a6c27fc1772dcb06b09ce3`.
Reference 02 lost the exact pair. This preference selects only the next exact
reference hypothesis for queue item `314606:54`; it is not a production
approval. Reference 03 must be bound into a new exact-ID workspace, rendered
once, validated and reviewed before its terminal outcome can be merged.

## Dobharchu results

The final publication is
`current-character-story-dobharchu-alternative-reference-v2`, comparison ID
`c33e83eec2982a453370b79ca57ba8f729f023fcb827b0cb177669bce0a90e8a`.
It tested each failed line first with its original exact portrait-cluster
reference and then with the other accepted Dobharchu portrait-cluster
reference:

- `reverse1999:314602:103:d579ac2a70771e37` was typed limited with both
  reference SHA-256 `c8f4928f6272...` and `130c9242bcd3...`;
- `reverse1999:314605:109:a5c710ca1debbf26` was typed limited with both
  reference SHA-256 `130c9242bcd3...` and `c8f4928f6272...`.

There is therefore no Dobharchu blind pair and no evidence that swapping the
two accepted expression-cluster references repairs these failures. Do not
spend another seed or silently merge the portrait controls.

## Remaining decision boundary

The one complete Narrator A/B trial selected reference 03 as the next bounded
hypothesis. The two unmatched complete Narrator WAVs may be heard as diagnostics
but cannot establish comparative superiority. The other four exact failures
still need a different bounded hypothesis or an explicit supported fallback.
The merged source state remained SHA-256
`c673b8631045c0d2a6206c6458f93b38b4b39e9b30b8efd3acd5ebbd893c2cf6`
after all publications.
