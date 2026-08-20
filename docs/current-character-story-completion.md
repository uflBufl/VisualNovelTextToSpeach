# Current Character Story completion sequence

This document records the fail-closed order for completing the current
`The You That's Meant To Be` authoring workspace. It is an operational plan,
not authority to generate, review, approve or publish any item.

## Verified checkpoint

The read-only checkpoint refreshed after the bounded legacy-failure
regeneration on 2026-08-21 has 592 queue items: 77 approved, 36 rejected, 129
generated and awaiting current-provenance review, 179 failed, zero ready but not
generated, 164 blocked by missing references and seven pure sound effects.
There is no active attempt, generation lease or partial WAV.
The queue SHA-256 is
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`
and the authoritative state SHA-256 is
`de93ffd0286be2b41f47689f97025d8290c950c5caf39939b262b26960c4c2d7`.

The previous cohort plan covered the earlier 128 pending WAVs. It is now stale
by design and must be rebuilt because one newly ready line produced an
additional pending-review WAV. No decision was applied during generation; the
approved-only manifest remains at 77 entries.

The earlier 140 pending WAVs had no provider/profile/control identity and could
not be relabeled. They were regenerated under the immutable current workspace
controls in one pilot, one batch of 10, five batches of 25 and one final batch
of four. Of those exact items, 128 produced validated PCM16 mono WAVs with
current provenance and 12 ended as typed bounded failures without a published
WAV. Every batch preserved the canonical hash of all non-target state items,
left no active attempt, lease or partial file, and did not change review
authority. The final pending-resolution plan is empty.

The 140 legacy failures were regenerated in six exact-ID batches of at most 25
items. Every item retained its old attempts under
`attempts_by_provider.legacy-unbound` and received exactly one seed-zero MOSS
attempt under the current immutable controls. All 140 attempts ended as typed
failures and published no WAV. Every batch compared all non-target state items,
left no active attempt, lease or partial file, and did not change review
authority. No legacy provenance was invented or written onto the old attempt
history.

The remaining ten reference-ready lines were then generated as one exact-ID,
zero-retry batch. One produced a validated pending-review WAV and nine ended as
typed missed-EOS limits; all 411 non-target state records remained canonical,
with no active attempt, lease or partial WAV. Seven of those failures have only
one current provider attempt and are eligible for at most seeds one and two in
a separate bounded-seed workspace.

That successor is
`resume-395a5e5eec0327a3a793b66d-15a0395f9ee3e3e3`. Its exact seven-item run
used only seeds one and two; all seven remained typed LIMITED with three total
MOSS provider attempts and no WAV. The final state SHA-256 is
`67dd14ab2cd629464c7fc83c4da1ccb97f43cf51781ef48f15998d95a13e1b52`.
All 338 non-target state items and the base state remained unchanged, with no
active attempt, lease or partial file. Those seven are now exhausted MOSS
outcomes eligible only for the existing one-attempt Pocket fallback or manual
reference comparison, not another seed retry.

All 179 failures therefore have current synthesis-control evidence. The
verified repair planner reports seven `bounded_seed_retry`, 68
`sentence_boundary_segmentation`, 75 `offline_fallback_backend` and 29
`reference_comparison` records. This classification is read-only and binds the
state and queue identities above; the zero-publish legacy result rules out
another broad MOSS seed retry.

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

1. Review the 14 new repair/ready-line WAVs. Run one unseeded Pocket attempt for
   the seven newly exhausted bounded-seed outcomes, then extend bounded
   specialist repair over the now-current 68 sentence, 82 offline-fallback and
   29 reference-comparison cohorts. Merge only exact
   terminal outcomes into a successor history. Preserve the 20-second ceiling
   and compare every non-target state item after a run.
2. Acquire and validate replacement references for Mrs. Owen and Hotelier, and
   build a successor Dobharchú comparison that addresses slow pacing and
   inter-phrase pauses. Human listening remains the authority for identity,
   pronunciation and contamination.
3. Generate the ten currently ready lines and newly unblocked missing-reference
   lines only after their controls are immutable. Apply checksum-bound cohort
   review to every new control combination.
4. Rebuild the approved-only manifest only from authoritative terminal state.
   Final game-pack publication remains blocked until every queue item has a
   terminal decision or an explicit supported fallback.
5. Run the real Character Story acceptance with the approved manifest: verify
   generated routing, original-audio precedence, Centurion narration, missing
   or failed live fallback and no stale/duplicate speech or early advance.

Every real mutation is a separately authorized controlled run. A failed or
bounded render remains failure evidence and is not silently retried.
