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
separately reports `2/4` terminal cohorts and 17 exact samples/items remaining
in its two unresolved reference cohorts after a later read-only reconciliation.
Its progress document is current and no decision was inferred for those
remaining WAVs.

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

The first exact failure-tail successor then selected only the 11 planner-safe
sentence segmentations and one final provider-local bounded seed from the
merged history. It ran with `retries=0`, base seed zero and produced no WAV:
seven outcomes remained typed `missed_eos_audio_limit` and five remained typed
`speech_silence`. The successor is idle with no lease or partial file, its
authoritative state SHA-256 is
`40b4a1637bec4fcea5fcd66002313f641d568cb9cfaac3de7788dfe5bc85d554`,
all 329 unrelated state records and all 197 pre-existing WAVs match the dry-run
twin, and the source history remains byte-identical. Its empty approved manifest
is correct for an isolated repair workspace; the 190 approvals remain in the
source history and were not copied or changed. The corrected read-only planner
uses the retained repair records to classify the exact tail as six
different-backend fallbacks, one final bounded MOSS seed, four reference
comparisons and one inline-pause comparison. It never authorizes the same failed
segmentation again or bypasses the three-attempt Pocket fallback gate.

The six immediately eligible fallbacks were then copied into isolated Pocket
workspace `resume-395a5e5eec0327a3a793b66d-d86e2dbf7e822dd1` and rendered
with one unseeded `pocket-tts/default` attempt per exact ID. All six completed,
passed the technical WAV gate and remain `pending_review`; no approval or
manifest entry was inferred. The workspace contains exactly six new 24 kHz
WAVs, no lease or partial file, and state SHA-256
`6fc3e548cdbcdc0062541e4af5826d35365c2a1527c12a312b6be2b9f1c6f928`.
All 332 non-target state records, 197 pre-existing WAVs and the source state
remain unchanged.

The one remaining provider-local repair ran separately in
`resume-395a5e5eec0327a3a793b66d-dccc2a67467e3e2c` with `retries=0` and
seed 2. It reached the typed 20-second/2,000-token MOSS limit without EOS,
published no WAV and exhausted the third exact MOSS attempt. The workspace is
idle with no lease or partial file and state SHA-256
`7dbdd9fff6c29295fc84bf9f9ac424caba4c5a5a44468523f7dc7b7b52c4fb5d`.
The planner must treat that applied exhausted bounded repair as stronger than
the line's generic multi-sentence shape and route its next successor to Pocket,
never back to sentence segmentation.

After that ordering gate was committed, the exact one-ID Pocket successor
`resume-395a5e5eec0327a3a793b66d-e39e8c434f50bc3f` rendered one unseeded
fallback successfully. Its 24 kHz mono WAV is 10.88 seconds long, has SHA-256
`c0eee5bb79bde5f642e1e851bc43cfbd4b289203e57f2defeec2b4cb3d304386`
and remains `pending_review`; its state SHA-256 is
`2a0aefb4546ef6d8bea314fbada5fdee1357c0a98ebad66cda092e91dd3639e0`.
The source MOSS state and every unrelated target item/WAV remained unchanged.

All seven final Pocket outcomes are collected without inferred decisions in
`review-bundles/current-character-story-final-pocket-fallbacks-v1.json`.
Bundle `692a8aa042fbb6c25a31765c5367d2bcdc2de5747099469cfb08cd5dee704e73`
contains three exact control/reference cohorts and all seven WAVs as mandatory
samples: five Narrator-source items using the selected Centurion reference,
one Rhiannon-source item and one Dobharchú-source item. Five samples carry
technical silence/pause or pace flags and two are deterministic clean controls.
Only explicit checksum-bound cohort review may approve, reject or expand them.

The operator completed that bundle on 2026-08-24. All three cohorts and all
seven mandatory samples were explicitly accepted; no decision was inferred.
The six-item source state SHA-256 became
`9f74f0097145f0d31f3b16cffb4373852e7416e82c912008043a4ffc854c3a52`
and the one-item source state SHA-256 became
`5c6606e2439103a1e320dc3ece4c4c656e1ae000679075a93fcddb2a94a0b734`.
The approved outcomes were atomically merged with the 190-approved base into
successor `resume-395a5e5eec0327a3a793b66d-cd54b7632c220de2`. It contains
197 approved, 209 generated and 15 failed items; its 197-entry manifest is the
exact approved state subset. The queue SHA-256 remains `1831f95d...`, every
copied WAV digest matches the review publication, and the successor has no
active attempt, lease or partial WAV.

The accepted Pocket evidence also bounds the review-only silence heuristic.
The seven accepted WAVs include silence ratios up to `0.2576` and longest
internal silent spans up to `1.12` seconds; the operator explicitly reported
that at least one `notable silence` label did not correspond to an abnormal
audible pause. Together with the completed Centurion review and rejected
multi-second Dobharchu evidence, this calibrated review-attention policy version
2 to `0.30` silence ratio and `1.0 s` internal pause. The accepted `1.12 s`
Pocket outlier remains intentionally selected for listening. These measurements
remain advisory sample-selection signals, not rejection verdicts, and the
stricter publication safety gate is unchanged. See
[`review-attention-silence-policy.md`](review-attention-silence-policy.md).

The next exact MOSS repair successor is
`resume-395a5e5eec0327a3a793b66d-25dc94e1e521dab8`. It selected only four
planner-authorized sentence segmentations and one final provider-local bounded
seed with `retries=0`. All five remained typed `speech_silence`; no WAV was
published. Its state SHA-256 is
`3d8dbe13a0bd2a7dd7ca6e1aca3e212d3e711ea2f16412c87c2970eec5917a2d`,
all 197 inherited WAVs and every unrelated state record retained their exact
baseline digest, the source successor stayed at `c673b863...`, and active,
lease and partial files are absent. Replanning from this exact state routes the
four failed segmentations to reference comparison and the bounded-seed result
to an inline-pause-marker comparison; none may be blindly retried or sent to
Pocket.

Creating the separately authorized seven-ID Pocket successor then exposed a
contract mismatch before publication: the planner correctly selected an
exhausted raw inline-pause-shaped `speech_silence` outcome, while workspace
construction accepted that transition only after an explicit inline-pause
repair had already been applied. Creation failed before a workspace or render.
The constructor and persisted/runtime provenance validators now accept the raw
transition only when the exact silence shape matches and the source provider
has at least three attempts. An end-to-end regression proves the resulting
Pocket outcome and source immutability; arbitrary silence remains rejected.

The resulting Pocket successor is
`resume-395a5e5eec0327a3a793b66d-dee61c5ea3baf68c`. Its carry-forward document
binds exactly seven failed records from source workspace `cd54b7632c220de2`
and source state SHA-256
`c673b8631045c0d2a6206c6458f93b38b4b39e9b30b8efd3acd5ebbd893c2cf6`;
the workspace intentionally seeds the immutable legacy snapshot rather than
copying the whole merged state. One unseeded Pocket attempt per selected ID
produced seven validated pending-review PCM16 mono WAVs and no review decision.
The final state SHA-256 is
`f18053ef0dfa4e56209a56ee3306a54d0975c13e6463cee30a5f663f79b8138f`.
The 332 non-selected seed records retained their exact baseline digest, the
workspace WAV count changed only from 197 to 204, the approved-only manifest
remained empty, the source state stayed byte-identical, and active, lease and
partial files are absent.

Only those seven new WAVs are published for review in
`authoring/review-bundles/current-character-story-exhausted-primary-pocket-fallbacks-v1.json`.
Its bundle ID is
`3cf27ce5ef86a6b52468ef795eca13a79a791464ec4b75cad759a9fef7fdc0cf`;
it contains one workspace, three cohorts, seven pending items, seven mandatory
samples and zero blocked items. Technical flags are advisory selectors and
listening remains authoritative. Terminal decisions must be merged back into
the 197-approved `cd54b7632c220de2` successor; the repair workspace is not a
replacement for that merged authority.

The separate inline-pause hypothesis for
`reverse1999:314605:9:1d0f968d85af2125` ran once in workspace
`resume-395a5e5eec0327a3a793b66d-c6dee25b528e1487`. It preserved the original
queue text/hash and recorded one derived 180 ms marker, but MOSS reached the
8.5-second/850-token limit before EOS. The terminal state SHA-256 is
`0f48987a2c3348e285ab73347f4162b6a33e9dedb07833f174ca6c5855d495b8`;
no target WAV or manifest entry was published, the source state remained
`3d8dbe13...`, and active, lease and partial files are absent. Another MOSS
seed or a larger limit is not authorized.

The exact planner then permitted one unseeded Pocket fallback. Its first
successor was published but rejected by public workspace loading before
synthesis because persisted validation assumed every inline-marker result must
remain `speech_silence`; this run had truthfully changed to
`missed_eos_audio_limit`. Planner, workspace and runtime validation now share
the narrow rule: an exhausted inline-marker source may carry either of those
two supported failure kinds into the already bounded one-attempt fallback.
Unsupported kinds and non-exhausted provider attempts remain fail-closed. The
regression executes the full silence -> inline marker -> missed EOS -> Pocket
chain and preserves the source state.

After that fix, the same config-addressed Pocket successor
`resume-395a5e5eec0327a3a793b66d-a2c30805e8846457` loaded idempotently and ran
one exact unseeded fallback. It produced a pending-review PCM16 mono 24 kHz WAV
of 3.20 seconds with SHA-256
`731fe7fa067553d562f92ee5ab7186f798e232cc15bcbc7dbfde16578114a0a3`.
The final state SHA-256 is
`3d517d1fc8807ea37587035c53820d1f027399d92e723a83e5b3084d9d1c3841`;
the marker source state remained `0f48987a...`, the approved manifest is empty,
and active, lease and partial files are absent. The measured 0.475 silence
ratio and 1.04-second internal span make listening mandatory, not automatic
rejection. Its separate one-cohort/one-sample review publication is
`authoring/review-bundles/current-character-story-rhiannon-inline-pocket-fallback-v1.json`
with bundle ID
`3b26c7811a6458dc1e610e405316cf688f99f21a06d3df74deec5d3dc6c2426f`.

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

The specialist, primary-risk, reference-quality, Dobharchú and terminal-conflict
reviews are complete. Their exact outcomes are composed in workspace
`resume-395a5e5eec0327a3a793b66d-0f0300f2c7b702ad`. The report-selected 46-line
bounded run completed from clean pushed commit `f8694a2` with selection-aware
readiness `selected=ready=46`, `retries=0` and `seed=0`. It produced ten
pending-review WAVs and 36 typed failures. All 421 non-selected state items
remained byte-identical, the lease and active attempt cleared, and no terminal
item was regenerated.

The ten WAVs are bound by bundle
`current-character-story-ready-fallbacks-v1.json`, bundle ID
`17d43bab998a4012e239a18b328637c388f1aace5ef3d91bf8ba1f3c015f5244`
and file SHA-256
`9c192b7b9fc37ff7a8acb1c5fda94d603feec884c096da062c0e4bda84f7f002`.
It has two exact control/reference cohorts and only six mandatory samples for
the three Hotelier-to-Centurion and seven Mrs. Owen results. It does not infer
approval from a technical pass.

The 36 failures split into 29 typed missed-EOS limits and seven typed
speech-silence failures. The deterministic repair classifier proposes 18 safe
sentence-boundary segmentation candidates, two inline-pause comparisons, 15
bounded seed candidates and one reference comparison. These are hypotheses,
not retry authorization. The current state SHA-256 is
`b00c516b92c82e546cd4c37304a03cb316d94d246596b2867ba667f794d29f14`.

Fresh reconciliation report
`184436748407f58fc644fe69de41934f8f5df29efe2baa59f0f6a353cc0b4317`
has zero terminal conflicts and reports ten `human_cohort_review`, 41
`new_hypothesis_required` and 118
`source_reference_or_explicit_fallback` actions.

The automatic bounded repair sweep then completed without changing the composed
primary or making review decisions:

- exact sentence repair for 18 failures produced five WAVs at attempt 2/seed 1;
- a separately carried final seed produced one more WAV at attempt 3/seed 2,
  leaving 12 MOSS failures in that branch;
- direct bounded seeds for 15 failures produced five WAVs at attempt 2/seed 1;
  the valid nine-item final seed produced none;
- the planner excluded four items whose latest outcome changed from missed EOS
  to speech silence rather than forcing the old repair class;
- one unseeded Pocket attempt for the remaining 17 planner-authorized,
  MOSS-exhausted missed-EOS items produced 17 WAVs.

All runs used exact queue-ID selections, `retries=0`, isolated config-addressed
successors and post-run equality checks for every unrelated state item. No WAV
was approved. Their review publications are:

- `current-character-story-sentence-repair-v2.json`, bundle ID
  `6fc099e2485893688d75deef11c08ee81ee6ef29b4ef9c7e95e84be5e51e268d`,
  file SHA-256
  `37e219c0d609b77db7946703ed0a8c75afa667768b0a12d7a863747f65d5c719`,
  two cohorts, six items, four samples;
- `current-character-story-bounded-seed-repair-v1.json`, bundle ID
  `193e184b3761b78f4b56644a8fdd3afb03cbbcaa9eac9b7026310c05b32724fd`,
  file SHA-256
  `9f66105a738e949e61c7aace82b61208e275726957fbd679ef61fdd62119fae0`,
  two cohorts, five items, three samples;
- `current-character-story-pocket-fallback-v1.json`, bundle ID
  `5049f5f3e36d504abda64166cc4ad85ab709a005f51c33550153a1b5e56d2961`,
  file SHA-256
  `11992e1c78a09bfb9c9d659e3a407201fedf71e1582017834e75bec0607b6541`,
  four cohorts, 17 items, 13 samples.

The first unfiltered sentence bundle exposed 197 unrelated legacy pending WAVs
as blocked occurrences. It had no observations or decisions and was moved
intact to `interrupted-review-bundles/overscoped-current-character-story-
sentence-repair-v1.json`; v2 is the authoritative exact-selection replacement.

Post-repair reconciliation report
`7a142c50fa9f621e76b16360f4832997c5a2f8680a5ded0ff772f652c95013a4`
(file SHA-256
`9e180d0f9270afcafeaf275ef5fac9b7e59b0a23e6ebc9e7214157c5618b8ad8`)
has zero terminal conflicts and reports 38 `human_cohort_review`, 41
`new_hypothesis_required` and 118
`source_reference_or_explicit_fallback` actions. The 38 review items are exactly
the ten direct results, six sentence repairs, five bounded-seed results and 17
Pocket fallbacks. They require 26 samples across ten cohorts, not listen-all.

The operator completed all four exact publications: ten of ten cohorts and 26
required samples decided all 38 WAVs. Every cohort was explicitly accepted. The
two direct decisions applied ten approvals to composed workspace
`resume-395a5e5eec0327a3a793b66d-0f0300f2c7b702ad`; the eight repair/fallback
decisions applied 28 approvals across five isolated successors.

Fresh reconciliation report
`901fe7a5f255b3433c4262e2763cece2247dbe87dbd590f2ca1d2c078f7fb359`
(file SHA-256
`c8ed6d8a8de6cbd6d68d1d50ffd52a9b096444b793dba3fdf7dccf6ec2443054`)
selected exactly those 28 secondary approvals, with zero terminal conflicts.
`merge-reconciled-outcomes` published successor
`resume-395a5e5eec0327a3a793b66d-9e3e40597ffc2a62`; an exact repeat returned
`created=false`, and all six source queue/state hashes remained unchanged. The
successor has 383 approved, 71 explicitly rejected and 13 failed state items;
its approved-only manifest has exactly 383 entries. Its workspace, state and
manifest SHA-256 values are respectively
`7701f38880be35925f1aa040209d23e6cd2be00e12e23f6f0fe5c25a042f66e2`,
`5fff2ebb50a5bf1bb9dc46c0a4194d3fb42b38dbe1e947802f7cb88efc6a93aa`
and `575532a9d58e54d59b4ae47e22d48108eb9622be9e943eb75f66202d0772cd49`.

Post-merge reconciliation report
`da65deb750c0f9bf5e1b074a368938a00b88b15872e57a37b6af3198fd8a7d5a`
(file SHA-256
`da78f797f92f1fc556ed6568243ee5c737bb0736520d9272be9a16afdda47fb8`)
has zero terminal conflicts, zero terminal merges, 13
`new_hypothesis_required` actions and 118
`source_reference_or_explicit_fallback` actions.

The subsequent exact reference-03 transaction resolved Narrator line
`reverse1999:314606:54:0450c81c4d1b3cc4`. Blind comparison selected Centurion
reference 03; one production-shaped render completed at attempt 4/seed 3 and
was explicitly approved under decision ID
`6ccdbe0df4f41174b3b627c33df7d44d142c2a617b33714ac6e71a5e240336b0`.
Its config-addressed successor
`resume-395a5e5eec0327a3a793b66d-5d48e1fefe53cf26` is now the composed primary,
with 384 approved, 71 rejected, 12 failed and 71 pending state outcomes and an
approved-only manifest of 384 entries. Fresh reconciliation report
`efa05a3fe7706a2983e170ce54c3b837bd451b8e597054ad59a700b02da084b4`
(file SHA-256
`601ff8118c1c42a3be4952c2f64a232a28d11b3dc678a9e52cbddcdf00e432a1`)
has zero terminal conflicts, 12 `new_hypothesis_required` actions and 118
`source_reference_or_explicit_fallback` actions.

1. Resolve the remaining 12 failures only through separately checksum-bound,
   evidence-backed hypotheses. Do not repeat an exhausted seed, repair or
   provider path.
2. Resolve the 118 missing-voice items through an exact reference or an explicit
   supported fallback; do not infer identity from a name alone.
3. Rebuild the approved-only manifest only from authoritative terminal state.
   Final game-pack publication remains blocked until every queue item has a
   terminal decision or an explicit supported fallback.
4. Run the real Character Story acceptance with the approved manifest: verify
   generated routing, original-audio precedence, Centurion narration, missing
   or failed live fallback and no stale/duplicate speech or early advance.

Every real mutation is a separately authorized controlled run. A failed or
bounded render remains failure evidence and is not silently retried.

## Current composed authority census, 2026-08-27

The later terminal merge successor
`resume-395a5e5eec0327a3a793b66d-9ec4454f35d082e4` supersedes the older
12-failure planning snapshot for current operational decisions. Its exact
queue SHA-256 is
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`;
its current state SHA-256 is
`a9c72e1fd345cd53c66e8b773374fac4b8005a0cd2ba59a15c188723b675fd0c`.
Public status reports 399 approved, 78 generated with explicit rejection, five
failed and no active attempt. The 78 rejected WAVs are terminal human outcomes,
not retryable generation failures.

The current typed failure-repair plan has exactly five records:

- Poacher II `reverse1999:314606:62:e3f44f0529c8ced0` is a
  `reference_comparison` action and remains bound to its separate blind
  reference task;
- Narrator `reverse1999:314606:43:09977e2b04515b66`,
  `reverse1999:314606:6:3511125b2e41a19f`,
  `reverse1999:314608:58:c3e23840e6ecc840` and
  `reverse1999:314608:94:f6c23264391ffae3` are
  `provenance_recovery_or_regeneration` actions. Their carried legacy failure
  records do not contain complete current provider/model/profile/control
  provenance, so no repair or terminal live fallback may be inferred directly.

Five formerly described Dobharchú failures are also explicit rejections in
this successor: `314605:102`, `314605:95`, `314608:8`, `314608:29` and
`314608:38`. They may remain rejected for live fallback or enter a new
checksum-bound reference comparison, but must not be silently retried or
counted among the five current failures.

The current-provenance recovery transaction then published immutable plan
`current-character-story-narrator-provenance-v1.json`, plan ID
`5b5591fee83b9889724b83265d6456d5e4485eccdd3946691ac50ccb7ac0661f`.
It selected exactly the four Narrator failures and excluded Poacher II and all
terminal outcomes. Its single child used MOSS stable, Centurion, current voice
controls, retries zero and provider-local seed zero. No WAV passed:

- `314606:43` ended typed `missed_eos_audio_limit` at the exact 14.5-second
  limit and is now a safe sentence-boundary segmentation candidate;
- `314606:6` ended typed `speech_silence` with one 3.12-second internal span
  between multiple complete sentences and is also safe to segment;
- `314608:58` and `314608:94` ended typed `speech_silence` with respective
  3.12 and 3.28-second internal spans inside single sentences, so neither is a
  safe segmentation candidate.

All four now separate three historical attempts under provider
`legacy-unbound` from one exact current `moss-tts` attempt. The new state
SHA-256 is
`a8839038d8560ecdbe93b1ccea45063d24e0d11fc864fb7ddf0a435e111205c6`.
All 478 unrelated state records retained aggregate SHA-256
`b96fda53b3d168521334f8d5218be3389f6619e03e412128ba634c56c52a45d5`;
the 477-WAV inventory retained aggregate SHA-256
`5a35e2534ee4274246af24886dc31c3b014f2fa25d976d5097c7df304c9eb1e4`.
The run ended with no active attempt, lease, partial WAV, approval or review
decision. The deterministic repair plan now contains two sentence segmentation
actions and three reference comparisons (the two single-sentence Narrator
failures plus the unchanged Poacher II item).

Isolated repair workspace
`resume-395a5e5eec0327a3a793b66d-a26944e66772019a` then carried only the two
safe segmentation actions from the current source. Its exact child selected
two queue IDs, used retries zero and published no WAV:

- `314606:43` rendered its two correct sentence segments at planned seeds 1
  and 2, but the combined result still failed with a 3.44-second internal
  silence;
- `314606:6` failed typed limited after the old splitter incorrectly produced
  `As a daughter, she ought to defend her mother.`, `But she knows Mrs.` and
  `Owen is right.` at planned seeds 1, 2 and 3.

The latter exposed a software defect rather than valid repair evidence. The
shared safe splitter and inline-pause transformer now ignore English
honorific/name abbreviations (`Mr.`, `Mrs.`, `Ms.`, `Dr.` and the documented
conservative set), repeated dotted abbreviations and capital initials. The
exact real line now produces only the two intended sentences, and focused plus
full regression suites cover that result. The failed workspace remains
immutable evidence; only a new config-addressed workspace with the corrected
two-segment plan may test the materially different hypothesis. The repair
workspace state SHA-256 is
`dcb4b8c0c3a92487dfe2a56e272c93c54df36ef010ca115d00139e38313d3c5c`;
it ended with no active attempt, lease, partial WAV, approval or review
decision.

The corrected `314606:6` hypothesis then ran once in isolated workspace
`resume-395a5e5eec0327a3a793b66d-4b31c8c7ae86f526`. Its exact plan contained only
`As a daughter, she ought to defend her mother.` and
`But she knows Mrs. Owen is right.`; the honorific was no longer split. MOSS
still ended typed limited with 408,000 samples, 36 chunks, the unchanged
20-second audio ceiling and 850-token ceiling. It published no WAV, partial,
review or approval and cleared its active attempt and lease. The final isolated
state SHA-256 is
`8ab718d3a1dc7c3e2b5b766e897732916f67305c72561f774ebbb33d6b05596a`.
Sentence segmentation is therefore exhausted for both `314606:6` and
`314606:43`; another seed or the old malformed plan is not a distinct
hypothesis.

The already completed unmatched alternative-reference WAVs for `314608:58`
and `314608:94` were copied into the self-contained reviews documented in
[`render-hypothesis-review.md`](render-hypothesis-review.md). This reused exact
evidence and did not rerender MOSS. The human rejected the `314608:58`
hypothesis because of its long pause. The human accepted the `314608:94`
reference-03 hypothesis and then approved its exact production WAV in isolated
workspace `resume-395a5e5eec0327a3a793b66d-8f248017c2917708`. The approved WAV
SHA-256 is
`59c6f5eb48c4204adc653d3da06f245a98926e6aace304514ba055b6ff9f68a8`;
the approved-only manifest contains exactly one matching entry, with no active
attempt or generation lease. Current composed successor
`resume-395a5e5eec0327a3a793b66d-a2b299862a4c4483` includes that exact approval;
it is not evidence to retry `314608:58` or either exhausted segmentation case.

The exact accepted `Tsk!` source event and its separately approved exact-copy
composition are now projected into reviewable successor
`resume-395a5e5eec0327a3a793b66d-a2b299862a4c4483`. Its workspace, state and
manifest SHA-256 values are respectively
`9fa9e59f09dabadc878695d8ac418e19b439fca619c210730a115c1cb17f1146`,
`2d839de3dd57e1a8aa1c487bb73a03dc96671a1c2a92a8397d26c43d17c5446d`
and `2d0e9cffa92fb16611c592362ca43fa49d4f8e154bf1f6a6eb31e917f1d69f57`.
The user's exact approval is recorded as `approved/approved`; the 401-entry
approved-only manifest includes the same checksum-bound composition ledger.
The successor preserves exact snapshots of base state
`f906935a13fd124ae10d95004c56145dd4f5a95a8cc29b8aa88504ab75392ba9`
and rejected WAV
`a7fcc6dd2c6b9f626f3301bfe63be16fc541094681a4b1a7ee9fecd8db0c6fcd`,
while its event WAV remains exact SHA-256
`492a92aa42f2e982a05974a96e8608b24cff50db38629aa2ebe6bb24cbb46634`.
No further `Tsk!` review or merge is required.

The evidence-bound live-fallback authority then terminalized the two exhausted
Narrator segmentation chains without synthesizing or approving Pocket WAVs.
Queue `314606:43` binds current failed item `47d859ac...` to exact repair result
`c95e02b8...` from workspace `...-a26944e66772019a`; queue `314606:6` binds
current failed item `abe8fa8f...` to corrected repair result `041de717...` from
workspace `...-4b31c8c7ae86f526`. Their schema-v2 decision SHA-256 values are
`f98c8b33bf2fcc4b2c3066628f25ef5941e8b3fc0d7cba28746afbdd91c42238`
and
`a4614e5172a745a151252d86d9f1751e33d26755ca77d0e942c46f00a713e1ee`.
The current state and approved-only manifest SHA-256 values are now
`fb1e27b9f30e07ae91f5d42e5acb2d50939283b9d3ff84645f6e9d7522a38dcb`
and
`20d86724b1d04f660761cdda8542219d5e229540be09b09431e9010355390c4e`.
Public status is 401 approved, 77 rejected generated WAVs, two explicit live
fallbacks and two remaining failures, with no active attempt. The only failures
are Poacher II `314606:62` and Narrator `314608:58`; both require their separate
reference-decision paths rather than another broad retry.
