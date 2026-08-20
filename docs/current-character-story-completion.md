# Current Character Story completion sequence

This document records the fail-closed order for completing the current
`The You That's Meant To Be` authoring workspace. It is an operational plan,
not authority to generate, review, approve or publish any item.

## Verified checkpoint

The read-only checkpoint refreshed after the bounded legacy-pending
regeneration on 2026-08-21 has 592 queue items: 77 approved, 36 rejected, 128
generated and awaiting current-provenance review, 170 failed, 10 ready but not
generated, 164 blocked by missing references and seven pure sound effects.
There is no active attempt.
The queue SHA-256 is
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`
and the authoritative state SHA-256 is
`065e2a7079669ea4d721f1c4a46fbded41831c5ae72f928fb595b8ea42e986c1`.

The current cohort plan now has three exact cohorts covering all 128 pending
WAVs, with 36 checksum-bound samples and zero blocked items. No decision was
applied during regeneration; the approved-only manifest remains at 77 entries.

The earlier 140 pending WAVs had no provider/profile/control identity and could
not be relabeled. They were regenerated under the immutable current workspace
controls in one pilot, one batch of 10, five batches of 25 and one final batch
of four. Of those exact items, 128 produced validated PCM16 mono WAVs with
current provenance and 12 ended as typed bounded failures without a published
WAV. Every batch preserved the canonical hash of all non-target state items,
left no active attempt, lease or partial file, and did not change review
authority. The final pending-resolution plan is empty.

The 170 failures now split into 140 legacy outcomes without complete synthesis
controls and 30 current outcomes with exact provider, model, profile and
synthesis-control provenance. A repair plan may prescribe a concrete strategy
only for the latter group. Legacy failures first require their own immutable
exact-ID regeneration plan under current controls.

The verified repair planner now reports 140
`provenance_recovery_or_regeneration` records and 30 executable current
repairs: 15 `sentence_boundary_segmentation`, 11 `offline_fallback_backend`
and four `reference_comparison`. This classification is read-only and preserves
the state and queue identities above.

`vntts-pregenerate pending-resolution-plan WORKSPACE --output PLAN.json`
atomically publishes a no-replace canonical plan for the cohort-blocked pending
WAVs. Every record binds the queue, line, text, state item and audio SHA-256 plus
the original blocker. Its only permitted action is
`provenance_recovery_or_regeneration`; creating the plan does not relabel a WAV,
change review state or authorize a render. Loading the plan revalidates its
schema, exact inventory, counts and canonical identity.

`vntts-pregenerate pending-regeneration-command WORKSPACE PLAN.json
--batch-index N [--batch-size 10]` recomputes the current resolution plan and
prints one bounded exact-ID `--regenerate-existing` child command only when the
workspace still matches it byte-for-byte. Batch size is limited to 25. The
command is inspection output: this operation does not launch the child, archive
an old WAV or change generation state.

## Execution order

1. Repair only the 30 current-provenance failures with bounded exact-ID plans:
   sentence-boundary segmentation, configured offline fallback, or explicit
   reference comparison. Preserve the 20-second ceiling and compare every
   non-target state item after a run.
2. Add a checksum-bound exact-ID regeneration plan for the 140 legacy failures,
   then regenerate them under current controls without assigning invented
   provenance to the old state records.
3. Acquire and validate replacement references for Mrs. Owen and Hotelier, and
   build a successor Dobharchú comparison that addresses slow pacing and
   inter-phrase pauses. Human listening remains the authority for identity,
   pronunciation and contamination.
4. Generate the ten currently ready lines and newly unblocked missing-reference
   lines only after their controls are immutable. Apply checksum-bound cohort
   review to every new control combination.
5. Rebuild the approved-only manifest only from authoritative terminal state.
   Final game-pack publication remains blocked until every queue item has a
   terminal decision or an explicit supported fallback.
6. Run the real Character Story acceptance with the approved manifest: verify
   generated routing, original-audio precedence, Centurion narration, missing
   or failed live fallback and no stale/duplicate speech or early advance.

Every real mutation is a separately authorized controlled run. A failed or
bounded render remains failure evidence and is not silently retried.
