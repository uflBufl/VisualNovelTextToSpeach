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
  COMPARISON_DIRECTORY --output LISTENING_DIRECTORY --seed SEED \
  --arm-id COMPLETE_ARM_A --arm-id COMPLETE_ARM_B
uv run vntts-pregenerate failure-reference-import-listening \
  FRESH_AUDIT_DIRECTORY COMPARISON_DIRECTORY LISTENING_DIRECTORY/session.json \
  EXACT_FAILED_QUEUE_ID
```

The operator-authored plan uses schema
`vntts.authoring-reference-render-input` version 1. Every arm lists the same
ordered `queue_id`, `case_group_id`, `candidate_group_id` and `candidate_id`
records; the loader rejects duplicate controls, changed audit identity and
cross-character candidate groups before model startup.

A comparison may retain more than two arms even when one arm ends typed limited
or cancelled. Blind listening always compares exactly two explicitly selected
complete arms. `--arm-id` selects their already published reports and WAVs from
the same immutable comparison; it neither rerenders them nor drops the failed
arm from comparison provenance. Omitting `--arm-id` is valid only when the
comparison itself has exactly two arms. The selected pair must share at least
one complete exact queue ID, otherwise session publication fails before write.

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
approval. The following transaction binds, renders and validates that
hypothesis; its result still requires a terminal human review.

That exact hypothesis was executed once on 2026-08-27. The fresh audit ID is
`655609916a99543ed0807d90fd5896268b272ea39c2ac14edf403bbdb869f4f5`;
the imported blind decision-set ID is
`87e574ab7359d85a04ca08f490d3d4d845f64189f32f07fd404ec13ab1283fba`;
and the immutable reference-binding ID is
`0777de073e4d4ce7362d908c4b0327f6d58cc8623a98ef381852aebbf105b688`.
The config-addressed successor is
`resume-395a5e5eec0327a3a793b66d-5d48e1fefe53cf26`. A preceding successor
created by the old fingerprint implementation was never rendered, had the
same state SHA as its base, and was moved intact under
`interrupted-workspaces/` before retrying from fixed committed code.

Only `reverse1999:314606:54:0450c81c4d1b3cc4` was eligible and rendered. It
advanced from attempt 3/seed 2 to attempt 4/seed 3 and produced a pending-review
PCM16 mono 48 kHz WAV with 372,480 frames (7.76 seconds), SHA-256
`cf0b114bbcaee8a8629c888d9fd8336c173b0a5819d06c27800265934ce934f2`.
Measured leading/trailing silence is zero, longest internal silence is 0.08
seconds and silence ratio is 0.0206. The exact review plan ID is
`157a0f3698be46ed308ac66064fdb0a1807cc6639175d1ef9b705633a2449897`;
it contains one cohort and one mandatory sample, flagged only for fast pace.
The other 591 state records retained canonical SHA-256
`c8406d8116b26b906bf5977fb8d69dd8553b5ad8289adc2fc52402a644046d01`;
no lease or partial WAV remained. This is still not an approval: the exact WAV
must be heard before an accepted or rejected decision is published.

The operator heard and approved that exact WAV on 2026-08-27. Immutable
decision ID
`6ccdbe0df4f41174b3b627c33df7d44d142c2a617b33714ac6e71a5e240336b0`
projects only the one queue ID and binds the WAV SHA-256 above. The resulting
successor is the new composed primary: it contains 384 approved, 71 rejected,
12 failed and 71 pending outcomes, while its approved-only manifest contains
exactly 384 entries. No second outcome merge is necessary because the
failure-reference workspace was already created as a config-addressed child of
the previous composed primary and preserved its full state. The final workspace,
state and manifest SHA-256 values are respectively
`379fd047188645af60013b3b1f4e4c3324e08110b3b6780ead778bc3a885d2d3`,
`b7d8218bce6d0e3e0c5b11befd6b33837801bdd4fb186c0cb20a5fb9667fdee8`
and `2a5cbe77ac57036b9b10bc13e0dc9c837719d1b6a96a039cf18305306e46c3ab`.
Fresh reconciliation report
`efa05a3fe7706a2983e170ce54c3b837bd451b8e597054ad59a700b02da084b4`
has zero terminal conflicts and reduces the failure tail from 13 to 12.

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

The one complete Narrator A/B trial selected reference 03, and its resulting
production-shaped WAV is now explicitly approved. The two unmatched complete
Narrator comparison WAVs cannot establish comparative superiority. They are now
copied, without rerendering, into the single-candidate checksum-bound reviews
described in [`render-hypothesis-review.md`](render-hypothesis-review.md). An
explicit `accept_hypothesis` permits only one subsequent production-shaped
hypothesis; it is not a line approval or speaker-wide preference. The other
current failure-tail items need a different bounded hypothesis or an explicit
supported fallback; none may inherit this reference decision by speaker or text
similarity.
