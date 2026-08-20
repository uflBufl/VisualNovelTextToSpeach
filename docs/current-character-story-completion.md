# Current Character Story completion sequence

This document records the fail-closed order for completing the current
`The You That's Meant To Be` authoring workspace. It is an operational plan,
not authority to generate, review, approve or publish any item.

## Verified checkpoint

The read-only checkpoint refreshed after the completed human cohort review on
2026-08-20 has 592 queue items: 77 approved, 36 rejected, 140 generated and
still pending review, 158 failed, 10 ready but not generated, 164 blocked by
missing references and seven pure sound effects. There is no active attempt.
The queue SHA-256 is
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`
and the authoritative state SHA-256 is
`0b95ea1b898d2ed749a958e47bf85e1ce0c2911bec5becc5da3214258c952109`.

The current cohort plan has no bindable pending cohort left. The 140 remaining
pending WAVs are legacy outcomes whose generation profile is absent. They must
not be batch-approved from a sampled cohort or silently assigned the current
profile.

The immutable legacy job records Centurion and the local MOSS model path, but
it has no provider or generation profile; the 140 state items also have no
synthesis-control digest. Current provenance therefore cannot be recovered
without invention. These exact items require regeneration under the immutable
current workspace controls.

The 158 failures split into 140 legacy outcomes without complete synthesis
controls and 18 current outcomes with exact provider, model, profile and
synthesis-control provenance. A repair plan may prescribe a concrete strategy
only for the latter group. Legacy outcomes first require immutable provenance
recovery or regeneration under current controls.

The verified repair planner now reports 140
`provenance_recovery_or_regeneration` records and only 18 executable current
repairs: 12 `sentence_boundary_segmentation`, four `offline_fallback_backend`
and two `reference_comparison`. This classification is read-only and preserves
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

1. Resolve the 140 legacy pending WAVs without a listen-all shortcut. Regenerate
   their exact IDs under current immutable controls in bounded batches. Never
   mutate the imported history or relabel old WAVs as current output.
2. Repair only the 18 current-provenance failures with bounded exact-ID plans:
   sentence-boundary segmentation, configured offline fallback, or explicit
   reference comparison. Preserve the 20-second ceiling and compare every
   non-target state item after a run.
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
