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

The one permitted Pocket successor is
`resume-395a5e5eec0327a3a793b66d-cb98c8cbe65621fa`. All seven unseeded Pocket
attempts produced validated pending-review WAVs; none was approved. Its final
state SHA-256 is
`c4ab7f6ffb64c91850a9a2f2e721acd41149b7033b3ec73d31591ce64e35307d`.
All 338 non-target items and the bounded source state remained unchanged, with
no active attempt, lease or partial WAV. Two earlier Pocket destinations that
failed closed before rendering were preserved under `interrupted-workspaces`;
they are diagnostics, not runnable histories.

All 179 failures therefore have current synthesis-control evidence. The
verified repair planner reports seven `bounded_seed_retry`, 68
`sentence_boundary_segmentation`, 75 `offline_fallback_backend` and 29
`reference_comparison` records. This classification is read-only and binds the
state and queue identities above; the zero-publish legacy result rules out
another broad MOSS seed retry.

The complete specialist repair pass finished on 2026-08-21 in ten final
config-addressed workspaces. Sentence segmentation covered 68 exact IDs and
produced 19 validated pending-review WAVs plus 49 typed failures. Direct Pocket
fallback covered 75 exact IDs and produced 73 validated pending-review WAVs
plus two typed failures. The seven exhausted bounded-MOSS items were carried
through an exact nested repair chain into Pocket and all seven produced
validated pending-review WAVs. This gives 99 unapproved specialist WAVs and 51
terminal specialist failures. The separate newly ready primary line gives one
additional unapproved WAV. The 29 reference-comparison items were not rendered
or relabeled.

The five sentence workspace suffixes are `cb751125876e4228`,
`e1d0de2b7d52fee0`, `28b0822ac8eb1f0d`, `2359eb370afc2402`, and
`b8ec90ce7a296823`; their pending counts are 3, 6, 4, 4, and 2. The five final
Pocket workspace suffixes are `ccceda27182925c9`, `eec99d4b176ec721`,
`69a686b6a33fbae3`, `84ab91224838c264`, and `cb98c8cbe65621fa`; their pending
counts are 10, 24, 25, 14, and 7. Two earlier nested-Pocket workspaces that
failed closed before rendering remain diagnostic archives, not review sources.
Every final run preserved non-target records and its source state, ended with
`active=null`, and left no lease or partial WAV. These specialist histories do
not yet merge their future review outcomes with the 77 approvals in the
primary workspace; that merge must be checksum-bound and explicit.

Building the existing conservative cohort plan independently for each final
workspace produces 18 exact cohorts and 81 required samples for the 99 WAVs.
The large sample is intentional: every technical-attention WAV is mandatory,
while only clean short/medium/long buckets are sampled. Opening ten independent
workbench windows is not an acceptable operator workflow, so the next software
boundary is a single review bundle that presents those exact source-bound
samples without weakening their authority or applying a decision across source
workspaces.

The unified version-2 bundle was published at
`authoring/review-bundles/current-character-story-specialists-v2.json` with ID
`f9131e035898a45c4aa36b509d5740ffaeace74ddd8a2a9e52f15a4ac95d8a8f`.
It contains exactly ten source workspaces, 18 cohorts, 99 pending items, 81
required samples and 197 unique inherited blocked items. Loading its exact
operator rows took 0.078 seconds, and an offscreen Qt acceptance showed all 18
cohorts and the first table in 0.399 seconds. Review decisions use the bound
state directly instead of rebuilding every full source plan; queue, workspace,
state, target item, WAV and lease authority remain fail-closed at commit time.

The 51 terminal specialist failures were then classified from their exact
queue, state, typed completion, repair strategy, text-shape and provider
evidence. The canonical plan is
`authoring/review-bundles/current-character-story-specialist-failures-v3.json`
with ID
`ff0b7551b1cfa7575232c985415adddc2e70e5081e16cec8287385847fe4cb27`.
Version 1 was rejected by the existing repair compatibility gate before any
child launched because it incorrectly grouped three complete MOSS silence
failures with missed-EOS limits. Version 2 was also rejected before any child
launched because nine limited items had only two cumulative MOSS attempts,
below the existing offline-fallback gate. Version 3 binds ten source workspaces:
nine limited sentence repairs permit exactly one more source-local MOSS
sentence attempt; 37 already exhausted limited items permit one unseeded Pocket
fallback; and five complete renders failed the speech-silence quality gate
(three under MOSS sentence repair and two under Pocket fallback). The five
quality failures permit only a verified reference comparison or live fallback,
not another blind render. This classification changes no state, WAV or review
decision.

Version 3 was executed in dependency order without relaxing either gate. One
of the nine exact third MOSS sentence attempts produced a validated
pending-review WAV; eight remained typed failures. Those eight plus the 37
already exhausted items then received their single unseeded Pocket fallback.
Pocket produced 44 validated pending-review WAVs and one terminal
speech-silence failure. Across all seven new branches, every source state and
non-target item stayed byte-identical, every run ended with `active=null`, and
no lease or partial artifact remained. No WAV was reviewed or approved.

The 45 new WAVs are isolated from inherited pending results by selected cohort
plans in
`authoring/review-bundles/current-character-story-specialist-followups-v1.json`
with bundle ID
`73b599327be6b0d837168eb9f190e6311cfac85b02217613cab6451c44f88439`.
It binds seven source workspaces, nine cohorts, 45 pending items and 38 required
samples. Selection is part of each source plan identity, is preserved through
refresh and expansion, and fails closed if an exact selected item is no longer
pending. The original version-2 bundle remains the review authority for the
earlier 99 WAVs; the follow-up bundle does not ask the operator to hear them
again.

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

1. Review the 99 original and 45 follow-up specialist WAVs through their two
   checksum-bound multi-workspace bundles plus the one newly generated primary
   WAV. Preserve
   source-local review authority and merge only exact terminal outcomes into a
   successor history. Keep all six terminal complete silence failures for
   reference/live fallback. Handle the 29 reference
   comparisons as a separate blinded decision task. Do not raise the
   20-second ceiling or run another broad seed sweep.
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
