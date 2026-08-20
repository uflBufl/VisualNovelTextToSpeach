# Current Character Story completion sequence

This document records the fail-closed order for completing the current
`The You That's Meant To Be` authoring workspace. It is an operational plan,
not authority to generate, review, approve or publish any item.

## Verified checkpoint

The read-only checkpoint captured at `2026-08-20T11:41:08+00:00` has 592 queue
items: 67 approved, 36 rejected, 150 generated and still pending review, 158
failed, 10 ready but not generated, 164 blocked by missing references and seven
pure sound effects. There is no active attempt. The queue SHA-256 is
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`.

The current cohort plan has no bindable pending cohort left. The 150 remaining
pending WAVs are legacy outcomes whose generation profile is absent. They must
not be batch-approved from a sampled cohort or silently assigned the current
profile.

The 158 failures split into 140 legacy outcomes without complete synthesis
controls and 18 current outcomes with exact provider, model, profile and
synthesis-control provenance. A repair plan may prescribe a concrete strategy
only for the latter group. Legacy outcomes first require immutable provenance
recovery or regeneration under current controls.

`vntts-pregenerate pending-resolution-plan WORKSPACE` produces a read-only
canonical plan for the cohort-blocked pending WAVs. Every record binds the
queue, line, text, state item and audio SHA-256 plus the original blocker. Its
only permitted action is `provenance_recovery_or_regeneration`; creating the
plan does not relabel a WAV, change review state or authorize a render.

## Execution order

1. Make repair planning fail closed for unbound legacy failures. Keep their
   exact queue IDs and failure evidence, but do not recommend segmentation,
   retry, fallback or reference changes until controls are bound.
2. Resolve the 150 legacy pending WAVs without a listen-all shortcut. Recover
   exact immutable controls when evidence exists; otherwise regenerate exact
   IDs in a successor config-addressed workspace. Never mutate the imported
   history or relabel old WAVs as current output.
3. Repair only the 18 current-provenance failures with bounded exact-ID plans:
   sentence-boundary segmentation, configured offline fallback, or explicit
   reference comparison. Preserve the 20-second ceiling and compare every
   non-target state item after a run.
4. Acquire and validate replacement references for Mrs. Owen and Hotelier, and
   build a successor Dobharchú comparison that addresses slow pacing and
   inter-phrase pauses. Human listening remains the authority for identity,
   pronunciation and contamination.
5. Generate the ten currently ready lines and newly unblocked missing-reference
   lines only after their controls are immutable. Apply checksum-bound cohort
   review to every new control combination.
6. Rebuild the approved-only manifest only from authoritative terminal state.
   Final game-pack publication remains blocked until every queue item has a
   terminal decision or an explicit supported fallback.
7. Run the real Character Story acceptance with the approved manifest: verify
   generated routing, original-audio precedence, Centurion narration, missing
   or failed live fallback and no stale/duplicate speech or early advance.

Every real mutation is a separately authorized controlled run. A failed or
bounded render remains failure evidence and is not silently retried.
