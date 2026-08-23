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
The reviewer numbers every remaining cohort as required, explains its
background save and checksum-refresh phases, keeps replay/navigation available
until authority actually changes, and removes each committed cohort before
selecting the next one. One shared state/queue snapshot per cohort reduced the
largest current 22-target authority capture from 0.0364 to 0.0096 seconds while
retaining exact item/WAV checks and a final source rehash.

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

The published tasks are resumable through the checksum-bound progress contract
documented in `authoring-workspaces.md`; use
`vntts-review-bundle BUNDLE.json --status` rather than inferring completion from
a past UI session. The original version-2 specialist task is now terminal
`18/18`. Its final expanded Narrator cohort reviewed all 19 exact samples,
retained two bad-sample assessments and rejected all 22 target WAVs under
decision
`8f76b441aed4f9f5f5c393a7df8eef1a406d06f60017a4f85b7efa0e3fe27b05`.
Read-only reconciliation reports zero remaining cohorts, samples and items for
that publication. The follow-up task is also terminal `9/9`: all 45 repair WAVs
were accepted across its nine exact cohorts. The two specialist publications
therefore contribute 102 approved and 42 rejected repair outcomes in total.
An isolated exact merge preflight over the 17 distinct source workspaces was
idempotent and produced expected successor
`resume-395a5e5eec0327a3a793b66d-d4fbbae41bdd4810`: 179 approved, 78 rejected,
129 pending-review and 35 failed state items, with 179 approved-only manifest
entries. The same successor was atomically published on 2026-08-23 and loaded
from its final app-data path. Its 17 recorded source-state hashes still match,
the outcome ledger contains exactly 144 terminal repairs, and the manifest is
the exact 179-item approved state subset. Generation, additional review and
final-pack publication did not run. The Dobharchú natural-expansion task
separately has all four cohorts and 24 samples/items remaining.

The 29 primary failures classified as `reference_comparison` are one separate
reference-quality decision, not 29 repeated auditions. Their immutable audit
groups exact failed-item identities by synthesis control: 23 Narrator cases
share three copied Centurion candidates, four Rhiannon cases share three
copied Rhiannon candidates, and the exact Aderyn and Poacher cases each have
one candidate. `vntts-pregenerate failure-reference-audit WORKSPACE --output
AUDIT_DIRECTORY` publishes a no-replace audit directory. Public candidate
labels are opaque; the private mode-0600 key maps them back to the original
manifest entries. The canonical audit identity binds that private inventory,
all copied audio hashes, exact failure records, queue, workspace and voice
manifest. Unrelated state reviews may proceed, while a changed audited failure
invalidates the task.

`uv run vntts-reference-audit AUDIT_DIRECTORY` provides the operator surface.
Playback reopens the copied candidate under full audit validation, reads it
once, verifies its SHA-256 and gives Qt an in-memory buffer. Candidate or
`Neither candidate is acceptable` decisions are written atomically by a
background worker and remain checksum-bound to the exact group and affected
queue IDs. They select reference evidence only: they do not approve generated
speech, mutate the manifest or authorize regeneration. The initial version-1
task was rejected before use because its private mapping was not included in
the public identity; only version 2 or later is valid for an operator decision.
The published version-2 directory is
`authoring/review-bundles/current-character-story-reference-audit-v2`, with
audit ID
`52fc3aa6e545109b79bbce7f1842ae5f1428c2f467521f362b3b09832f53223e`.
It contains 29 exact cases, four control groups, six blinded candidate pairs
and no decisions at publication time. Its private key is mode 0600. A real
offscreen UI open validated all source authorities and exposed four groups. The
operator surface centers the current opaque candidate, keeps affected line IDs
collapsed by default, records a session-local heard ledger and enables the
candidate/Neither decisions only after every candidate in the current group
reaches end of playback. Navigation and playback remain available outside the
short background decision commit.
The operator completed all four groups on 2026-08-23. Read-only reconciliation
reports `4/4`, zero remaining groups and canonical decision-set ID
`fc519819f6f7a30fc0474199ac12d9192a9a9e0fd9b855b1f1c70888b5424893`.
Unblinding against the exact mode-0600 key selected Rhiannon
`references/base/01/01.wav` (`5bb83cc7...`), the sole Aderyn anchor
(`cc3d4d1d...`), the sole Poacher anchor (`42c1ccbf...`) and Centurion
`references/narrator/01.ogg` (`ddcef063...`). These are complete reference
decisions for all 29 cases, but they are still evidence rather than a voice
manifest/control binding or regeneration authority.
`vntts-reference-audit AUDIT_DIRECTORY --status` performs the same public/key/
decision validation without constructing a Qt application or writing a missing
`decisions.json`; it reports the audit ID, completed/remaining group counts and
the canonical decision-set ID when decisions exist.

The selected-reference overlay implementation was dry-run against the exact
terminal version-2 audit and latest trusted successor on 2026-08-23. It
produced binding ID
`eb4e323fb8f1a5381e92cae72d4fa6846a3f12f21ec856c7f04775787737181e`.
The binding owns exactly 29 queue IDs across the four terminal groups and keeps
the selected reference hashes above. Its config-addressed dry-run successor
was `resume-395a5e5eec0327a3a793b66d-4e0189bc2bb0090d`. The successor's entire
`generated-audio` tree was byte-identical to base
`resume-395a5e5eec0327a3a793b66d-d4fbbae41bdd4810`: 179 approved, 207 generated
and 35 failed items, with no active attempt. Exact selected readiness was
29/29 ready, zero missing voices and no blocker; the derived bounded command
contained exactly 29 queue-ID arguments, `retries=0` and base `seed=0`.
This dry-run lived under `/private/tmp`; it did not publish app-data or start a
model. Real publication and generation remain gated on a clean committed tree,
idempotent no-replace publication and a repeated source inventory check.

The real no-replace publication subsequently completed from clean commit
`99ea6d8`. The binding lives at
`authoring/reference-bindings/current-character-story-reference-binding-v1`;
an exact repeat returned `created=false`. The real config-addressed successor is
`resume-395a5e5eec0327a3a793b66d-c34e5d54d994e53a`, and its repeat was also
idempotent. Before generation, its full generated-audio tree matched the base
byte-for-byte. The source audit remained 11 files with aggregate inventory
`8b8e47fa...`; the base remained 406 files with aggregate inventory
`112d1a5a...`; the five-file binding inventory is `6f236d5e...`. Exact selected
preflight again reported 29/29 ready, zero missing voices and no blockers.

One bounded exact-29 run then used `retries=0` and base seed zero. It produced
two pending-review WAVs for `reverse1999:314604:23:fee7b4775afa6761` and
`reverse1999:314607:72:61924bf530710fa3`; 27 selected items remained failed.
No review decision was made. All 392 unselected state records and all 386 base
WAVs remained byte-identical, approvals and approved-manifest entries remained
179, and the process left no active attempt, lease, partial WAV or source-tree
change. The resulting selected failures are 22 typed missed-EOS limits and five
typed speech-silence failures. The read-only repair planner partitions those 27
as 14 sentence segmentations, eight offline fallbacks, three new reference/
silence investigations, one inline-pause comparison and one final bounded seed
retry; these are separate evidence paths, not authorization for a bulk retry.

The one planner-authorized final Aderyn retry for
`reverse1999:314602:4:721879c12bb3873b` completed as another typed missed-EOS
limit: attempts 2 -> 3 and seed 1 -> 2, with no WAV, partial, active attempt or
lease. It is now the ninth exhausted-primary offline fallback; no MOSS seed
retry remains in this selected-reference cohort. Because the first exact
two-item cohort plan bound the pre-retry state SHA, it remains immutable but is
superseded. The current operator task is
`authoring/review-bundles/current-character-story-selected-reference-results-v2.json`,
plan ID `20aabd2f03d1f86315437ff2265dab6da2fccb05b4c391af9b45be9f840944d7`.
It binds post-retry state SHA `d6039278...`, exactly two one-item Centurion/
Narrator cohorts and both new WAV hashes; it contains no inherited pending
items and no technical flags.

The first real fallback-successor dry-run then exposed a planner/constructor
disagreement without touching app-data: cumulative attempts mixed
`legacy-unbound` and `moss-tts`, while safe fallback authority is provider-local.
The constructor correctly rejected a Rhiannon item with five total attempts but
only two MOSS attempts. The planner now uses the same provider-local boundary.
The corrected 27-item partition is 14 sentence segmentations, seven final
bounded MOSS attempts, two exhausted-primary Pocket fallbacks, three unresolved
Narrator silence investigations and one Poacher inline-pause comparison. The
temporary sentence workspace was only a dry-run artifact under `/private/tmp`;
no repair model ran.

The four bounded follow-up runs then completed from clean commit `7409954`
without applying a review decision. Seven final provider-local MOSS attempts in
the selected-reference successor produced one validated Narrator WAV and six
typed failures. The 14-item sentence-boundary successor
`resume-395a5e5eec0327a3a793b66d-8f5c3a4daeb3ec75` produced seven validated
WAVs and seven typed failures. The two-item Pocket successor
`resume-395a5e5eec0327a3a793b66d-4f8501cbe5c1438a` produced both its Aderyn and
Narrator WAVs, with one provider-local Pocket attempt and no applied seed. The
single Poacher inline-pause successor
`resume-395a5e5eec0327a3a793b66d-c0cee8df625c405a` produced its validated WAV
with a bound 180 ms marker policy. Every run used `retries=0`, changed only its
exact selected state records, preserved every pre-existing WAV byte, and ended
with no active attempt, lease or partial WAV. The three Narrator
reference/silence investigations were not rendered because no new bounded
hypothesis exists.

The resulting operator task is the immutable bundle
`authoring/review-bundles/current-character-story-selected-reference-repairs-v1.json`
with ID
`419a2118159812a7a44218ad84d851f9af6b926607b4f48c5ad8fa2851f7fb63`.
It binds four source workspaces, eight exact cohorts, 13 pending items, 11
required samples and zero blocked items. Its exact inventory contains the two
original selected-reference WAVs, the one final bounded-MOSS success, seven
sentence-repair successes, two Pocket fallbacks and one inline-pause result.
The earlier two-item version-2 plan remains immutable but became stale when the
final bounded-MOSS run changed its source state; it must not be used for a new
decision. The seven failed sentence-repair outcomes and the three untouched
Narrator investigations remain failure evidence. They require a new explicit
hypothesis rather than another automatic attempt.

The operator completed the selected-reference repair task on 2026-08-23.
Read-only reconciliation reports `8/8` completed cohorts, all 13 exact WAV
decisions applied and zero remaining samples or items. Eleven WAVs were
approved and two sentence-repair WAVs were rejected. The approved outcomes are
the three direct/final MOSS results in the selected-reference workspace, five
sentence repairs, two Pocket fallbacks and the Poacher inline-pause result.
The two remaining reviewed sentence repairs retain their exact rejected
authority; no decision was inferred for any unreviewed failure.

An exact outcome-merge dry run then used
`resume-395a5e5eec0327a3a793b66d-c34e5d54d994e53a` as the primary history and
the Pocket, sentence-repair and inline-pause workspaces as its three reviewed
sources. The same merge was atomically published under the application data
root as
`resume-395a5e5eec0327a3a793b66d-1c6ff408bbe999e0`; an exact repeat returned
`created: false`. The successor has 190 approved, 209 generated and 22 failed
state items. Its 190-entry manifest is exactly the approved state subset, its
queue SHA-256 remains `1831f95d...`, and its final state SHA-256 is
`f8d60aa0ac88b07617bb8bbbeaf02cd684888db00e1010e36148ad015e6e9481`.
It has no active attempt, generation lease or partial WAV. The merge copied
only exact terminal reviewed repair outcomes, revalidated every source
workspace and left all source histories unchanged. It did not run generation
or publish a final game pack. The seven failed sentence repairs and three
untouched Narrator investigations remain blocked on a new bounded hypothesis.

The first operator save attempt on a binding-backed cohort exposed a
review-time fingerprint regression: cohort application omitted the immutable
`failure_reference_binding` field while workspace creation and loading included
it. The false mismatch failed closed before writing review evidence. Read-only
reconciliation after the error still reported all eight cohorts, 13 items and
11 samples remaining. Review application now uses the same complete canonical
fingerprint as the workspace boundary, with a binding-backed commit regression
and the existing binding-tamper rejection tests.

The reference-audit dialog also offers an optional generated sample for the
current opaque candidate and one exact affected line. It renders in a
background worker with the workspace backend, model and generation profile,
deterministic seed zero and bypassed persistent audio cache. Candidate bytes
are copied from a read-once SHA-256-verified payload into a private ephemeral
directory; generated PCM is normalized to mono and replayed from immutable WAV
bytes. The dialog keeps only a lifetime-local memory cache, revalidates the
candidate and complete audit after rendering, supports request cancellation and
shuts down its isolated model worker on close. It never writes generation
state, a manifest, binding or audit decision. Hearing or liking the generated
sample therefore informs, but never performs, the separate source-reference
decision.
An exact real-model smoke on 2026-08-23 used the selected Rhiannon candidate,
the shortest affected line (`That's not. I wasn't trying to—!`), the bound
local MOSS model, stable profile and seed zero. It completed at 48 kHz and
produced a 453,164-byte ephemeral WAV with SHA-256
`7984b3e013a91a8905c4a5df784357a522d51e45a9b8deb3e404b641d9237a98`;
the service then shut down and retained no preview file or authoring write.

`vntts-pregenerate pending-resolution-plan WORKSPACE --output PLAN.json`
atomically publishes a no-replace canonical plan for the cohort-blocked pending
WAVs. Every record binds the queue, line, text, state item and audio SHA-256 plus
the original blocker. Its only permitted action is
`provenance_recovery_or_regeneration`; creating the plan does not relabel a WAV,
change review state or authorize a render. Loading the plan revalidates its
schema, exact inventory, counts and canonical identity.

Accepted cohort controls may be carried as a reusable baseline through the
separate contract in [`voice-quality-gates.md`](voice-quality-gates.md). A
matching gate still requires the later story's technical-attention and clean
sample and never carries per-WAV approval.

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
