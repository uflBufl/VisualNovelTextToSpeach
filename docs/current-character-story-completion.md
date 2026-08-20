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

The first bounded current-failure repair pass completed on 2026-08-21 in two
separate config-addressed workspaces. Sentence segmentation produced three
validated pending-review WAVs and retained 12 typed bounded failures. Pocket
fallback initially exposed and then closed a seeded-request contract bug; the
diagnostic workspace was preserved under `interrupted-workspaces`, and a fresh
workspace was recreated from the unchanged MOSS source. Its one backend-owned
unseeded attempt per ID produced ten validated pending-review WAVs and one
typed speech-silence failure (1.52 seconds internal silence, 51% silent
frames). All 13 new WAVs remain unapproved. Both runs ended with `active=null`,
no lease or partial WAV, and an empty approved-only repair manifest. The four
reference-comparison items were not rendered or relabeled.

The sentence workspace is
`resume-395a5e5eec0327a3a793b66d-cb751125876e4228`, with final state SHA-256
`fca03e0fcf7e818dc9ffaa696be0a801f7a3f5c91dde6aa8b169696d7ae048a7`.
The Pocket workspace is
`resume-395a5e5eec0327a3a793b66d-ccceda27182925c9`, with final state SHA-256
`f7aff27e5cef754c8d6cee5b4e4e5c1a906202eab3c159492e7c6734f97002f8`.
These specialist histories do not yet merge their future review outcomes with
the 77 approvals in the primary workspace; that merge must be checksum-bound
and explicit.

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

`vntts-pregenerate failure-regeneration-plan WORKSPACE --output PLAN.json`
does the equivalent for current failed records that lack exact synthesis
controls. Each record binds the workspace/config, queue, state and failed item
SHA-256 plus its old attempt/seed evidence. The plan contains only the
`provenance_recovery_or_regeneration` cohort; it never assigns a provider,
model or control digest to the old result.

`vntts-pregenerate failure-regeneration-command WORKSPACE PLAN.json
--batch-index N [--batch-size 10]` revalidates the complete plan and prints one
exact-ID `--regenerate-existing --retries 0 --seed 0` child command, with a
maximum batch size of 25. When an old failed record has no provider, its old
attempt count is preserved under `attempts_by_provider.legacy-unbound`; the
first newly proven provider attempt starts at seed zero. The command is
inspection output and does not launch generation.

## Execution order

1. Review the 13 new repair WAVs, resolve the 13 remaining typed failures and
   four reference-comparison items without broad retry, then merge only exact
   terminal outcomes into a successor history. Preserve the 20-second ceiling
   and compare every non-target state item after a run.
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
