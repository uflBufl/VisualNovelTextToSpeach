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

Narrator `314608:58` was then terminalized from its exact rejected
render-hypothesis review, not from the presence of its WAV. Decision schema 3
binds current failed item `71bc1d75...`, review `51600bc4...`, review file
`40d1844f...`, terminal `need_different` decision file `d4119041...`, Centurion
reference `d2be6ca0...` and rejected result `7ff11a60...`. Its live-fallback
decision SHA-256 is
`174fca4e629e96f38543003be7032272692e7f9f0ad4ad6098c1a6eb355d6429`.
The current state and manifest SHA-256 values are now
`d7e090f44aa06f7d0abff68c8d4b0bbdc212ce277889d56550e71b8cf5395169`
and
`15e13019176c0f3fead1f7fa2389472a45a2b90bb40c7122422370b2b24e2d2b`.
Public status is 401 approved, 77 rejected generated WAVs, three explicit live
fallbacks and one remaining failure, with no active attempt. The only failed
item is Poacher II `314606:62`; its blind reference task remains the sole
generation-failure decision gate.

The five already reviewed Dobharchú rejections then received explicit
schema-v1 `generated_audio_rejected` fallback ledgers. Their rejected WAV bytes
and `generated/rejected` statuses remain unchanged; no audio was promoted or
regenerated. In queue order `314605:102`, `314605:95`, `314608:8`, `314608:29`
and `314608:38`, the decision SHA-256 values are `4985cde4...`, `990efd41...`,
`eb9b9740...`, `2b10b49f...` and `813bfb91...`. The current state and manifest
SHA-256 values are
`f11ab9e2e97438c1b651f95f252d75fa45fde6d03508b713c7e4b01fdb2bd0a4`
and
`ee60f3b7b9e96bbb1afd1e6828a4b95b03ac5dc81f040a4e3bec21230c0529dc`.
Status now reports 401 approved, 77 rejected generated WAVs, eight explicit
fallback decision ledgers and one failed item, with no active attempt. The
status command counts fallback ledgers attached to rejected WAVs as well as
standalone `live_fallback` state items.

Attaching those five ledgers exposed an overly strict config-rebase projection:
the independently validated `live_fallback` field and its new `updated_at`
timestamp made `inspect_workspace` reject the otherwise intact successor. The
validator now accepts only a `generated_audio_rejected` extension whose
`previous_result_sha256` exactly matches the reconstructed original rebased
item. It still requires the original status, review, WAV, `config_rebase`
authority and every projected synthesis field to remain unchanged. Malformed
fallbacks, changed base hashes and any other item mutation remain fail-closed.
The real workspace loads again, and the full 1,487-test suite plus Ruff pass.

The resulting full queue census is no longer inferred from state count alone.
All 592 queue entries use action `generate`: 582 are spoken and ten are typed
non-verbal events. The state contains 482 outcomes, leaving 110 absent. Of the
103 absent spoken lines, eight are ready now: six exact Mrs. Owen bindings and
two Hotelier lines under the explicit Centurion Narrator fallback. The other 95
are intentionally blocked: 93 spoken Aderyn lines lack an age/portrait-safe
route after the child variant's real-story rejection, while Dobharchú
`314608:95` and `314608:96` use unbound portrait `534705`. The seven absent
non-verbal events are `*whimper*`, `*yelp*`, `*pop*`, three `*bang*` lines and
`*buzzzzz*`; approved `Tsk!` and the two rejected mixed `*gasp*`/`*gurgle*`
outputs account for the other three typed events.

The exact eight ready spoken lines then ran in the composed workspace with
seed 0, no retries and no regeneration of existing outcomes. All eight ended as
typed failures and published no WAV: five `missed_eos_audio_limit` and three
`speech_silence`. The generation summary skipped the other 584 queue items and
left `active=null`. The resulting state and manifest SHA-256 values are
`04a59cc5baf0884abe102489fda8a794db9bb334ac0f950631af440d21548434`
and
`4bedd679f855281eb0b65c502e3920592f7b13be5eeaf1088a1087b7e7ef402e`.

Planner-authorized repairs ran only in immutable child workspaces. Workspace
`resume-395a5e5eec0327a3a793b66d-f24bd8763ea2287c` carried five exact failures:
three safe sentence-segmentation cases and two bounded seed retries. All five
seed-1 attempts failed without a WAV. Its state and manifest SHA-256 values are
`ddba1a3a24e086a170c229bae378d6456ed82d6bedc42920e7a9a6f1042afb7c`
and
`c46b2304cd11800159d4c6241904a957379eec53d6c923190a8a4e47a1447e72`.
The planner allowed one final current-provider attempt for four remaining
`missed_eos_audio_limit` lines; Mrs. Owen `314608:86` instead moved to the
comparison path after a 3.04-second internal-pause failure.

Workspace `resume-395a5e5eec0327a3a793b66d-b9716bf5de811152` carried only
those four final bounded cases. Every seed-2 attempt completed audio but failed
speech-silence validation with a 2.88-3.28-second internal pause, so no WAV was
published and no review cohort exists. Its state and manifest SHA-256 values
are `7468cde2004f751395b51c7ba78409ddd42975b56bfad3f6ba76d078d8a447fd`
and
`c7afc8e1db3f20d7259df319ab7f153fb9dedc624df1e47dc51ad4e8b4661b0a`.
The typed planner now requires reference/listening evidence for all four and
will not authorize a fourth MOSS seed. Together with the four earlier
pause/reference cases, all eight Mrs. Owen/Hotelier lines now require explicit
comparison evidence rather than more automatic production attempts.

The remaining known-role missing-voice scope is now an immutable comparison
plan rather than a 95-line manual review request. Plan
`current-character-story-aderyn-missing-voice-reuse-v1.json` has ID
`a5a0cf1969f1ed4c4b11ea2e8592dccfa2796da0e11298abf28e28bc94b9e96f`.
It binds all 93 absent spoken Aderyn lines to two explicit review scopes: 53
lines with the ten observed `3146xx` portrait IDs and 40 lines with the three
observed `5337xx` IDs. These are review families only, not an assertion that
neighboring portrait numbers are the same image or voice. The plan compares the
active adult Aderyn source reference, the three-reference Rhiannon voice and
Centurion; the retired child source-reference variant is excluded. Deterministic
short, medium and long sampling reduces the future human gate to six lines.

Plan `current-character-story-dobharchu-534705-missing-voice-reuse-v1.json` has
ID `c373c52cfb7bbf61b6238cdf9d353b9dab5ab7567422a39e9bffe84c2afb02f0`.
It preserves the two exact `534705.png` lines as one separate review scope and
compares the active `534703` and `534704` Dobharchú references with Centurion.
Both lines are samples because only short and long buckets exist. Neither plan
changes a queue, state, manifest binding or production selection; candidate
workspaces and blinded rendered evidence are required before any family can
inherit a voice.

The bounded candidate generation and planner-authorized repair pass is now
complete. Every initial run used seed zero and `retries=0`; a failed sample was
retried only when the typed failure planner selected one bounded seed or safe
sentence segmentation. Inline-marker and `reference_comparison` cases were not
silently folded into the trial, and no third seed was spent. The adult Aderyn
candidate published one of six samples after safe sentence segmentation;
Rhiannon published one of six direct samples; Centurion published zero of six.
The remaining arms are typed failures, primarily `missed_eos_audio_limit`, so
neither Aderyn review family has a complete selectable candidate. This is
negative evidence, not permission to choose from the successful subset.

The candidate preparation path also closes two predecessor-composition gaps.
A successive config rebase preserves an already validated
`generated_audio_rejected` live-fallback ledger byte-for-byte while rebinding
only the new config projection; any changed review, WAV, status, decision
ledger or unrelated result field fails closed. It likewise carries the exact
workspace-level audio-event composition authority and every referenced input
when an approved composed item is carried, and rejects conflicts or missing
composition inputs before publication. This keeps sample workspaces loadable
without weakening earlier terminal decisions.

For Dobharchú portrait `534705`, the `534704` reference and Centurion each
published the short sample but not the long sample. The `534703` reference
published both exact samples, including a 7.12-second long line with measured
0.24-second maximum internal silence, and is the only complete candidate. Its
identity remains hidden from the operator until the decision is imported.

The immutable Aderyn review bundle has ID
`a1c72942984c2849434a2e205188dbfc339b8b1291112c7b873bdc350e17a976`
and lives at
`current-character-story-aderyn-missing-voice-reuse-v1-review`. The immutable
Dobharchú bundle has ID
`91b9e2b76dfb22329d45a97113a7e0ef5bcd491592015f995510c69ed6c4e450`
and lives at
`current-character-story-dobharchu-534705-missing-voice-reuse-v1-review`.
Each public bundle contains a complete opaque `candidate x sample` matrix:
generated arms bind copied WAV hashes and repair strategy; failed arms retain
their typed kind and attempt count and never disappear from the comparison.
Candidate selection is enabled only when the candidate completed every exact
sample in that cohort and every available cohort WAV reached end of playback.
`Neither` also requires all available WAVs to be heard. Replay and fixed
previous/next controls remain available while the small decision ledger is
saved in the background.

`vntts-pregenerate missing-voice-reuse-review-ui SESSION.json` opens this
surface, while `missing-voice-reuse-review-status` performs read-only
validation. After all cohort decisions are terminal,
`missing-voice-reuse-binding PLAN.json SESSION.json --output DIRECTORY`
publishes a no-replace successor voice bundle. A selected opaque candidate is
unblinded through the mode-0600 key and bound to every exact target queue ID in
that cohort, not merely the listening samples. A `Neither` decision is retained
as an auditable zero-override authority. The importer rechecks the current
source workspace, plan, review bundle, session, key, candidate references and
all copied manifest inputs; it cannot regenerate or approve a WAV.

Both Aderyn cohorts later resolved automatically to `Neither` because none of
their three candidates had complete exact sample evidence. The no-replace
zero-override bundle is
`authoring/missing-voice-reuse-bindings/current-character-story-aderyn-missing-voice-reuse-v1-unresolved`,
decision ID
`8239afefb72e57fcd112f7ff17ad1ad56877839f7dd4e6bc10285842dd008a07`.
It binds both cohort identities, retains zero queue voice overrides and does
not authorize generation or reuse for any of the 93 Aderyn lines. Those lines
therefore remain known-role unresolved and require an explicit Narrator/live
fallback decision rather than another candidate audition.

The same exact matrix now supports exhausted failed controls. Three immutable
Mrs. Owen -> Centurion plans bind the final authorities separately: b971 plan
`e0ac3de0eacec6111d24cd3a822c29f4fd32c9bb1fded98fd9bc1d7205fe6f06`
contains three final seed-2 failures and two deterministic samples; f24 plan
`9a7f240e44f2875e20b607d77dab0948c984342b22c776ecbeb33e108b6d1d11`
contains `314608:86`; a2 plan
`5c70f04b58b26b4d3f0c3cd9e29d7f127f7f55b72ec5c746fbf1b7537b35f4f2`
contains `314603:13`. All bind the same three-reference Centurion candidate and
retain each original speech-silence item as a non-playable checksum control.

Four seed-0 comparison attempts were run in fresh workspaces, without carrying
or incrementing the source failures. Only one b971 sample completed; its cohort
candidate is therefore incomplete. The other b971 sample and both single-line
samples failed, so no plan currently has a selectable Centurion candidate.
Review bundles
`ee14f9a400daf81fb1af18c3f9f560a66ea50b66895cf2ee6b58152adb4cddec`,
`7413adc95cf34819ca5ce70201d5c9e052658210c6ed070cab19da6b815c3e11`
and
`274c6afb2e8cfb11e4a68b8427075278ac67a242a5ee0b3e5afa3ac1da23e50c`
make that negative evidence explicit. They permit only an explicit unresolved
decision; this result does not authorize another Mrs. Owen seed.

### Hotelier composite and exact prompt-hypothesis evidence

Hotelier's exact-bank composite is now available only through comparison input
bundle `4a894f700555e44c1631ee05ec22cc095154f41326440d61de6c004f2cc37e42`.
It binds composite WAV SHA-256
`79c43a24a232d7ac853c13c93b553fa40cdd652b7dfa9926a988cff9a35b3293`,
the composite/evaluation ledgers and the self-contained quality review whose
decision remains `needs_sample`. The added voice is marked `experimental_only`;
the bundle preserves the predecessor queue overrides exactly and adds no route
or production binding.

The normal config rebase correctly refused to treat a failed render as a
terminal audio decision. Dedicated failed-control carry
`83f1ca4c8137271d85ed2198f5efeab137c899b38ae2d0ca541a41a22d5a699d`
therefore copied only Hotelier `314601:47`'s exact non-playable state item into
workspace `resume-395a5e5eec0327a3a793b66d-d39dacd78d1abdce`. It required a
byte-identical queue and identical Centurion reference hashes and copied no WAV
or new attempt. Failed-control plan
`f896d39501f41279d57c6c24c7e57dae5a82f8203faa1e4799416b12e505f514`
then rendered exactly that one line in workspace
`resume-395a5e5eec0327a3a793b66d-dc4eaa0b8aeaa213`. The composite candidate
completed synthesis but failed with three 2.80-2.88-second internal pauses and
58% silent frames, so no WAV was published. Review bundle
`ead3879cc5ecd57ddc7eb201a13e4e57a0ae235c00d8d99e51b9080496d2d0e0`
has no selectable candidate and permits only `Keep unresolved`.

The same failed-control matrix now supports canonical inline-pause prompt
hypotheses without misrepresenting them as voice changes. Candidate identity
binds the original text hash, derived prompt hash, marker count and pause
duration. Candidate preparation carries the exact failed control into a fresh
workspace, and the generation command must contain matching single
`--inline-pause-failed` and `--queue-id` values. A dedicated selection importer
can publish only the human hypothesis choice; it is forbidden from creating a
voice binding, changing generation state or approving speech.

Hotelier plan
`d8054c0028b6bb7deee16d223d40caee5f2325ab5c192a5e731d813bc8af5b3b`
binds one 180 ms marker and derived prompt SHA-256
`b25d0e3e924c2f037a04751cd6ff16e2ecdc2b199220c5e9e04f990736975ef4`.
Its workspace `resume-395a5e5eec0327a3a793b66d-a8b3063e388147cd`
reached the exact 12.5-second audio limit and published no WAV. Mrs. Owen plan
`2a1b11de27e97a50fa5b32fc805028ad0e636185fbda2a425201f0aeaddb5538`
binds two 180 ms markers and derived prompt SHA-256
`ab39d152f44725b9d9a8effb48414c3d0fdc7dc1fff84bc244492dae83f5384c`.
Its workspace `resume-395a5e5eec0327a3a793b66d-9d1cf3971c4701ac`
reached the exact 17.1667-second limit and likewise published no WAV. Review
bundles `eac5c068a97aad54a06e29af1d24483f23d6f2e6210684ad8d011a0d18fab8d9`
and `0cc87c5ff53b3395e3f3a5ac1d6c42017e38e7a459b11e7d8630df0ff61faa9b`
retain those typed failures and expose only `Keep unresolved`. These results
close the bounded marker hypotheses; they do not authorize another MOSS seed.

On 2026-08-28 the zero-choice gate was corrected: a cohort with no complete
selectable candidate is now deterministically unresolved and requires no human
playback or acknowledgement. The four exact failed-voice reviews were imported
as zero-override bindings with automatic origin and complete failed-state hash
authority. Their decision IDs are:

- Mrs. Owen b971: `e6472e1852125a3fa3e59e1eeb00902624425d5b87c45a730b57aef245246a5b`;
- Mrs. Owen f24: `12d7a2638f98dbddbf8b3bcb3bf0fa2f607c0a77dd016478706596fcdbbc851f`;
- Mrs. Owen a2: `c64cf89cb03aea5086cbaf5514060b5c2be4aa40df4114fe9ec42230ca18ea65`;
- Hotelier exact-bank: `3341721416d5b6f9e50eb2b8a502037fac50bf39ab188c9c431c368e2294d4ea`.

The two failed inline-pause hypotheses were likewise imported as unresolved.
Hotelier selection ID is
`cbf652b3f95ad0e94d129613bcf73a44b2985b6f0c508fa2441e8b1dc47af759`;
Mrs. Owen selection ID is
`0e20bb4cb7ab927e4a4668c9e569edd389cbbcc7928d51273854390c2b602334`.
These six artifacts close the bounded MOSS experiments but publish no audio and
authorize no voice route. They were subsequently used only as exact fallback
authority for six immutable Pocket workspaces:

- Mrs. Owen b971 (three lines):
  `resume-395a5e5eec0327a3a793b66d-500c3f83dec7ae82`;
- Mrs. Owen f24: `resume-395a5e5eec0327a3a793b66d-81b24450fd3dbd50`;
- Mrs. Owen a2: `resume-395a5e5eec0327a3a793b66d-044308790f726c99`;
- Hotelier exact-bank:
  `resume-395a5e5eec0327a3a793b66d-5f629aebc0cb3bec`;
- Hotelier inline-pause:
  `resume-395a5e5eec0327a3a793b66d-4049955dac5517b8`;
- Mrs. Owen inline-pause:
  `resume-395a5e5eec0327a3a793b66d-dfe4c049b57617d5`.

Each workspace copies its canonical authority into immutable provenance,
retains the existing Mrs. Owen source-reference or Hotelier Narrator ->
Centurion route and permits only one unseeded `pocket-tts/default` attempt.
All eight exact attempts produced technically valid `pending_review` WAVs; no
result was approved. The current checksum-bound task is
`authoring/review-bundles/current-character-story-pocket-fallback-v2.json`,
bundle ID
`a54c2b70b422ffa500540dd69ee45167d7686739420cef30026e976419bfa69b`.
It contains six source workspaces, six cohorts, eight pending items, eight
required samples and zero blocked items. Five samples carry advisory `fast
pace` attention and three are deterministic clean samples; listening remains
the decision authority. The older `v1` path is stale against predecessor state
and must not be used. Final coverage remains gated on this v2 review and an
exact approved/rejected outcome merge.

The operator completed v2 with all six cohorts and all eight exact WAVs
explicitly approved. `merge-workspace-outcomes` now accepts both schema-v3 and
schema-v4 repair sources; schema-v4 sources still pass the full copied
automatic-unresolved authority validation before any reviewed item can be
selected. A positive merge regression and a tampered copied-authority
regression preserve that boundary.

The isolated preflight and real no-replace publication both resolved to
successor `resume-395a5e5eec0327a3a793b66d-fefe4656d7d30e02`; exact repeats
returned `created=false`. The real successor has 592 queue items and 490 state
outcomes: 409 approved, 77 generated/rejected, one failed and three standalone
live-fallback items. Public status counts eight explicit fallback ledgers in
total because five are attached to rejected WAVs. It has no pending review or
active attempt. The remaining absent scope is exactly 95 missing-voice lines
and seven unresolved non-verbal events; the only failed item remains Poacher II
`314606:62`.

The real workspace, state and approved-only manifest SHA-256 values are
`bce23126f919b79992edc963b3d36d67fd88fd42a57df7e861c27004e7e7dfee`,
`e5983ad3a2b648214758ffa2f03fbaf3ff93deb24be6c23edaa06bf56bc7e07d`
and
`20832d94d0c145e3954dd4d34ac360430ea3f508171ce4f8112861ce03541a6b`.
All six source workspace and state hashes matched their preflight snapshots
after both real publication calls; no source review authority was mutated.

The terminal Aderyn zero-override authority now also has a safe atomic live
fallback importer. A real read-only preflight against current successor
`resume-395a5e5eec0327a3a793b66d-fefe4656d7d30e02` validated both automatic
`Neither` cohorts and all 93 still-absent Aderyn queue identities. It reported
authority decision
`8239afefb72e57fcd112f7ff17ad1ad56877839f7dd4e6bc10285842dd008a07`,
batch ID
`67bd260baaceef5931b8ee2712da643559f62a565141d5c8deb87c179d7e8f11`
and unchanged before/after state SHA-256
`e5983ad3a2b648214758ffa2f03fbaf3ff93deb24be6c23edaa06bf56bc7e07d`.
No fallback was applied. Applying it remains one explicit human policy gate;
the importer requires `--accept-known-role-narrator-fallback`, commits the full
scope under one lease and cannot accept a partial queue selection.

## Explicit Aderyn -> Rhiannon identity authority, 2026-08-28

The user subsequently established the story identity policy `Aderyn ->
Rhiannon`, including the three lines formerly assigned to the rejected child
portrait variant. This supersedes the prepared but unapplied Aderyn Narrator
fallback. It does not retroactively approve any generated speech.

No-replace bundle
`authoring/known-role-reuse-bindings/current-character-story-aderyn-to-rhiannon-v1`
has decision ID
`3e2137ab2d89f2aadf06ca573e8ac80dbad6828f73978d98bbffb457bd5be2e7`,
bundle ID
`98d1f9d08337cb8a09f4702697a0a2ca09567d77f535808455236949167f2685`
and bundle-document SHA-256
`4f0963a65a7a198391df70c09b854f26d31ae073e361065b711a990530ae79ac`.
It binds 104 exact queue/text/speaker identities to the three-reference
Rhiannon voice: 93 previously absent lines and 11 rejected lines, including
the three IDs from retired child variant
`cluster-6a3c52e451a4abb5a69c32a8-anchor-1`. Nine existing approved Aderyn WAVs
remain outside the override and retain their reviewed source route. The bundle
copies and validates every voice reference named by its manifest; the first
incomplete publication was preserved beside it with suffix
`invalid-incomplete-references-3e2137ab` and is not valid authority.

Config rebase version 3 gives an exact known-role route change a safe state
transition. Only a rejected item whose canonical state hash is named by the
binding becomes pending in the successor. Its old decision and WAV move into
the self-contained config-rebase source snapshot, while unrelated terminal
decisions remain active and protected. This avoids both overwriting a terminal
review with `--regenerate-existing` and losing the negative evidence. The real
successor is
`resume-395a5e5eec0327a3a793b66d-40a7a5a24af271d0`; exact rebase repetition
returned `created=false`. Before generation it contained 104 pending Rhiannon
overrides, nine preserved Aderyn approvals, 11 historical rejected WAVs and no
Aderyn live or Narrator fallback. The source workspace and state hashes still
matched the binding after publication.

One bounded MOSS stable seed-0 pass with no retries processed all 104 pending
items. It produced 43 technically clean `pending_review` WAVs and 61 typed
failures: 50 `missed_eos_audio_limit` and 11 `speech_silence`. None of the 43
WAVs carries analysis-version-2 technical-attention flags. The deterministic
repair planner classifies the 61 failures as 13 safe sentence-boundary
segmentations, 42 bounded seed retries, two inline-pause comparisons and four
reference comparisons. No retry or repair result is implied by this census.
The workspace, generation-state and generated-manifest SHA-256 values at this
checkpoint are
`0b5f21190be3006b9e15c32f77afc1c0fcc45794fabf5063735494683ee19850`,
`3ea19804b28ed30a9472089d24030075a2dd2869772e763f9c54c1a6c398d5a7`
and
`d1ea5affdb5a1e8477f93a18da09efe0f18f3bb1eed6dc80e3c51b82eaa500fe`.

The 13 planner-authorized sentence-boundary cases then ran in isolated repair
workspace `resume-395a5e5eec0327a3a793b66d-706e025553318f5f`, again with
seed 0 and no retries. Six produced technically clean pending-review WAVs;
four still ended `missed_eos_audio_limit` and three ended `speech_silence`.
Re-planning permits one bounded current-provider seed for the four missed-EOS
items and sends all three silence failures directly to reference comparison.
The repair workspace, state and manifest SHA-256 values are
`9ba664c7d59ea9f9994cd3c0ea1ced307dca5070dacec2634c6dd47fc8a4b94e`,
`396a86eb7852967d28ea44c24f6e714effe97038198f0d900f12fb137c3376b4`
and
`378a7d3c1dae5aeea9b4a234a6f59a2400d8679fdae308525fc521cd017ab661`.

The next bounded MOSS seed ran in two source-specific workspaces. Base-failure
workspace `resume-395a5e5eec0327a3a793b66d-cbecf377c313d430` processed 42
exact single-sentence cases and produced seven technically valid WAVs. Its 35
remaining failures re-plan to 31 final bounded seeds, three reference
comparisons and one inline-pause comparison. Post-segmentation workspace
`resume-395a5e5eec0327a3a793b66d-d96c8e798db08e17` processed four cases and
produced no WAV; three now require reference comparison and one has exhausted
MOSS and permits only the typed offline fallback. The first workspace/state/
manifest hashes are
`34d42ae20da5782db4dc2389f230465ed52d3206f0f6598a92c4ec3790aca631`,
`b3536b3734d8578f7d6189e6b21f11d01c366bbdb2a36623d34783439eb78055`
and
`21deab7d9bf374993048cf25673f94483489fb0ba9cfb968f2d70bb30170ca8d`;
the second set is
`b35ddff833cccdd2a65223e8880d7f5419bb7250e7a42e92c0a1726b54ff7969`,
`542c6b75a4f808a988a462f43aaa448826c637344a7a59420243cc31b2c643e5`
and
`18064e93ecbbe472d52f5251d72f21c4b9118edd2522efbad98e688c41ada283`.

Final bounded-seed workspace
`resume-395a5e5eec0327a3a793b66d-f9b67c8120c30d93` processed the 31 remaining
planner-authorized cases at their third and final MOSS attempt. Four produced
technically clean WAVs; 27 exhausted the current provider and now all classify
as `offline_fallback_backend` (24 missed EOS and three speech-silence). The
workspace/state/manifest hashes are
`e924c0765ca7497550e8f7e8fa663b2b658fc752dcc98566a404e901f8085c37`,
`1d9c68c938914fdf9def777bb526a38ed1c2e9f2163a5341c5c7ec3e9da8cc84`
and
`f8bad26e8c20809c60c4d590ed6070650f9f40b1697af3aed243d6cc8e33f9d9`.
Across the Aderyn identity branch, 60 of 104 exact Rhiannon-routed targets now
have technically clean MOSS WAVs. The unresolved 44 split into 28 typed offline
fallbacks, three inline-pause comparisons and 13 reference comparisons; no
further MOSS seed is authorized for the 28 exhausted cases.

The 28 exhausted cases then received their one permitted unseeded Pocket
attempt in two source-specific workspaces. Workspace
`resume-395a5e5eec0327a3a793b66d-8cb0f74eaca6cf10` produced 22 valid WAVs and
five final Pocket failures; workspace
`resume-395a5e5eec0327a3a793b66d-8aa15e352591f8ed` produced its single valid
WAV. None of the 23 outputs has analysis-version-2 technical-attention flags.
Pocket preserves the exact Aderyn speaker and requested Rhiannon route in its
provenance, but it is a generic fallback voice, not a clone of Rhiannon, so it
requires a separate perceptual cohort from MOSS. The branch now has 83 valid
pending WAVs and 21 unresolved targets: five exhausted Pocket failures, three
inline-pause comparisons and 13 reference comparisons.

The retired child portrait is an explicit quality stratum. Its two valid MOSS
outputs and one valid Pocket output are published in
`current-character-story-aderyn-rhiannon-child-production-v1.json`, bundle ID
`0dc7d440f0d594cc142ac229ca779122cda67515db0dd36bcb7d242569ae2d6a`
and file SHA-256
`ae87aa21e5b3f3bc93391e40ac4db1c51d1024838d61f4fc145737aff4e0ca92`.
The bundle contains exactly two workspaces, two cohorts, three pending items
and three required samples, so no child line can be approved through an adult
sample.

The child review completed both cohorts on 2026-08-28. Decision
`cc1e9142bfd6c87aba818aa8b8fe6a45f75911e15cabcc6b26b115410f83f383`
approved both MOSS WAVs, including the 5.52-second result for
`reverse1999:314604:72:721879c12bb3873b`. Decision
`6102910f6af6012cb2f20c3401c0b82e733832d343dc4fd692d7fe521e18f47e`
rejected the one Pocket fallback WAV for
`reverse1999:314604:77:a848a1190ada63a4`. The progress successor contains
zero cohorts and zero pending items; its file SHA-256 is
`6bc9d5a7e5ff3d2bde2e6d80b14e96c9896ddf2dadb53194beacf8b93017a8dd`.

With the child gate terminal, immutable adult review bundle
`current-character-story-aderyn-rhiannon-adult-production-v1.json` was
published with bundle ID
`ae20b0835e50df46396d60297b21c32b92af13473c3885f51f001b6b0f02418a`
and file SHA-256
`e07b33957d133a34013ac2dfdcecd89c5d39ef3b30bffcc7f107267274d3bb68`.
It binds exactly 80 still-pending adult Aderyn/Rhiannon WAVs from six source
workspaces: 58 MOSS and 22 Pocket results. Exact per-workspace selection keeps
all 197 unrelated inherited legacy pending records outside the task. The
bundle contains six cohorts and 36 required samples; no adult decision has yet
been applied.

The adult review subsequently completed all six cohorts. It approved 57 of 58
MOSS WAVs, rejected the remaining MOSS WAV and rejected all 22 generic Pocket
WAVs. Together with the terminal child review, the 83 newly reviewed Aderyn
WAVs therefore contribute 59 approvals and 24 rejections. These decisions were
merged with the nine prior Aderyn approvals into immutable successor
`resume-395a5e5eec0327a3a793b66d-af9b4fb0bb4a451c`. Its workspace, state and
approved-only manifest SHA-256 values are
`62406d00c7780658b4ef1a949298074a2336744fcbf3f699d013328a4ea15905`,
`a292099ecb6a8b5b46f472e66482fcdd8e9046246cc49482c909749d98b43704`
and
`dc505ebe61f206196e5206018208af3ae6f05981f02ab60cf8b17240cfb34c0e`.
The validated whole-workspace summary is 468 approved, 88 rejected, 24 failed,
one pending, five explicit live fallbacks and two missing-voice lines, with no
active generation. The Aderyn slice has 68 approved WAVs, 24 rejected WAVs and
21 failed records.

The merge initially failed closed because the supported direct-repair allowlist
omitted `bounded_seed_retry`, even though bounded outcomes are first-class,
checksum-bound repair results. The allowlist now includes that strategy and a
direct reviewed bounded-result regression test protects the path. A separate
temporary-root preflight produced the same config-addressed successor before
the application-data publication.

The remaining 21 Aderyn failures must not be replanned from the merged
successor alone: terminal-outcome merge deliberately leaves nonterminal base
records untouched, so its generic repair projection does not contain the
deeper attempt history from the repair branches. Their exact authorities are:

- five final Pocket failures in
  `resume-395a5e5eec0327a3a793b66d-8cb0f74eaca6cf10`; no additional MOSS seed or
  second generic Pocket attempt is authorized;
- three inline-pause comparisons: `314605:80` and `314606:65` from
  `resume-395a5e5eec0327a3a793b66d-40a7a5a24af271d0`, plus `314603:58` from
  `resume-395a5e5eec0327a3a793b66d-cbecf377c313d430`;
- 13 alternative-reference comparisons: four from `40a7a5a24af271d0`
  (`314603:18`, `314603:52`, `314605:54`, `314607:14`), three from
  `706e025553318f5f` (`314603:77`, `314605:103`, `314608:13`), three from
  `cbecf377c313d430` (`314602:97`, `314605:39`, `314607:15`) and three from
  `d96c8e798db08e17` (`314603:60`, `314604:9`, `314605:90`).

Reference 01 remains the accepted Rhiannon reference. References 02 and 03
both produced large pauses in the corrected fixed-text comparison, so the 13
reference cases are evidence-blocked until a new safe reference hypothesis
exists; they do not authorize a broad 02/03 generation batch.

The five final Pocket failures are now bound by specialist plan
`current-character-story-aderyn-pocket-exhausted-v1.json`, plan ID
`ff5d7d49ad6ffb56d770dbfd0a7f96208ac88beca0d29f55cc0d783f6a9921a7`
and file SHA-256
`23b2257a07f067213be2699c907589c2bd1c76756b329f46fe4153d963f6b579`.
All five select only `reference_comparison_or_live_fallback`; the plan does not
authorize another Pocket attempt.

The three inline-pause hypotheses were split into the required single-ID
plans. Plan IDs for `314605:80`, `314606:65` and `314603:58` are respectively
`b340776c2cb2cb3012ed09b534c54ca038c37a44667ff483c9de70bbca6dea84`,
`b03b244b7d6beaa46f45d83242d18e90983183bec10ef1479ee7784f47fd2f63`
and
`1fb627186ff5815f083496f4643e4dad7b9bd49a23b80a7fde709946db03c38e`.
Each uses a 180 ms canonical marker, stable MOSS and the existing Rhiannon
controls. `314605:80` and `314603:58` reached their typed audio limits
without publishing WAVs; their automatic unresolved selection IDs are
`4ce7be2bf2c27b49c85de6ec466aa10da1635854cfda33e7837fbe31b5743f68`
and
`06fad43f38288bf2a9619863e17b24324f141dc057eb5decb4d3fce7be3a52ae`.
`314606:65` produced its 4.72-second pending WAV in candidate workspace
`resume-395a5e5eec0327a3a793b66d-dc187d97a6cb802a` on its second bounded attempt,
seed 1, with SHA-256
`06a616bfe87ef15d250718e7b809e144ec84969e120582d2ee0d01b431bee871`;
its one-cohort review bundle ID is
`25518bfff0badfdf0fca162e37a18935ad1f81ed36b7ed3407adef267a104519`.
The user heard that exact WAV and selected candidate A. The imported selection
contains one selected item, zero unresolved items and selection ID
`5f861cf2826928d173f08959c9f48fb33f6edcf940569d9d31920642a2bf92d6`.
It selects only this checksum-bound 180 ms inline-pause result; it does not
authorize the two typed-limited hypotheses or any broader regeneration.

Candidate creation exposed two reusable fail-closed composition gaps. A
failed-control comparison may now replace an earlier missing-voice binding
only when that predecessor is a fully validated zero-override `Neither`
authority. Its overlap with an explicit known-role route is allowed only when
the failed item hash is present and both layers resolve to the same normalized
voice. Conflicting voices and non-empty predecessor overrides remain rejected.
Inline-pause planning also enforces the existing one-queue-ID contract before
publication rather than failing later during workspace construction. The
superseded two-item plan and its copied input remain recoverable under
`interrupted-review-bundles/invalid-aderyn-inline-pause-multi-id-83388fc8`.

### Rhiannon first-reference decision, 2026-08-28

A corrected fixed-text comparison rendered `I offer my flesh as pledge: grant
me sight of this world and the worlds beyond!` independently from each of the
three base Rhiannon references with MOSS Local 4B, stable profile and seed 0.
The comparison did not combine references. The checksum-bound results were:

| Reference | Reference SHA-256 | Result duration | Result SHA-256 | Decision |
| --- | --- | ---: | --- | --- |
| `voice-02/01.wav` | `5bb83cc73fae544e12945c563d820da4b7ffee5f97cebd4e16c79f1d9a3b8778` | 5.52 s | `9c39e2e3d57edad1fcdfb604fe02cac2ae359fd906b4a5eb225d9cee1e31c363` | accepted |
| `voice-02/02.wav` | `76527ff41e12301b51879c1830eadeebeb374e627fd37b038a09ccd7f25340cc` | 11.52 s | `4b6d229ffb5d3feca294d537db46839554e67abe3ffc1ce27e9b0b31ea85604d` | rejected: large pauses |
| `voice-02/03.wav` | `7b3b1a1da981255a2c64a840a3f9972132ef717a1e8683c383a2b4314a34e008` | 10.88 s | `a08a201a1875f13841bb6e22b3de9e6902e06885647b10a5053b7d07da691a19` | rejected: large pauses |

The accepted result from reference 01 is byte-identical to the existing
production WAV for queue ID
`reverse1999:314604:72:721879c12bb3873b`. Keep the current first-reference
MOSS policy and do not promote references 02 or 03 to the first conditioning
position. No production regeneration or workspace mutation is required.

The first ad-hoc export of this comparison was invalid: it flattened
frames-by-two-channel PCM into the time axis and therefore doubled WAV duration
and produced slowed, distorted playback. The corrected export used the same
channel downmix as `_generated_mono_pcm`; invalid temporary WAVs are not review
evidence. Production has enforced this invariant since commit `81073bf`.

## Dobharchú 534705 binding and production review, 2026-08-28

The completed opaque missing-voice review selected candidate C after all four
available WAVs were heard. The no-replace importer unblinded it as source
reference variant `cluster-2f4d52a49d13c24bbd0e74ad-anchor-1`, the existing
`534703.png` Dobharchú voice, and bound exactly
`reverse1999:314608:95:965bd814a6e36dbf` and
`reverse1999:314608:96:5e1fe5bdc801e728`. The published binding directory is
`current-character-story-dobharchu-534705-missing-voice-reuse-v1-selected`;
its decision ID is
`1f50973ab7de0e5a46e60afe181efcae27f30bda742090bc661970a0b2f716ed`.

The Dobharchú overlay was composed with the explicit 104-item `Aderyn ->
Rhiannon` authority through the reviewed additive-overlay gate. The combined
known-role bundle lives at
`current-character-story-aderyn-to-rhiannon-plus-dobharchu-534705-v1` and has
decision ID
`d92e855eaae2c32b7927a7d93f1895557039a7f58ffe2aee042e7f59060d5b87`.
Config rebase preserved the current Aderyn history and produced workspace
`resume-395a5e5eec0327a3a793b66d-55e3fc75baa0668c`. Its validated routing map
contains all 104 Rhiannon overrides and the two new Dobharchú overrides in
addition to the existing source-reference routes.

One exact seed-0 MOSS run generated both Dobharchú lines. The long line is 7.12
seconds with 0.24 seconds maximum internal silence and WAV SHA-256
`56529ef9f28601953f05298f4ad60b45f0f870d43623272be1465f757d2dd91e`;
the short line is 1.44 seconds with no internal silence and WAV SHA-256
`e11aa573bf97413accc86ac08e79c86bf6d842425f24d383aea3ef1cb2c1bbfe`.
Both were accepted in human quality review through exact bundle
`current-character-story-dobharchu-534705-production-v1.json`, bundle ID
`0cc19a3e391e3dd15402375bf3f69028fd6283f5c6b1ca5ee27c85f791f5dead`.
Decision
`8ef47a7688aec761920cb4668dd460ce727791e9aeb42f2052a918c715d74a02`
accepted both checksum-bound WAVs after the short result's fast-pace flag was
heard rather than treated as an automatic rejection.

The earlier Poacher II file
`current-character-story-poacher-ii-production-v1.json` was a schema-v1
cohort plan, not a review bundle, so `vntts-review-bundle` correctly rejected it
and could not show the line. The corrected schema-v2 publication is
`current-character-story-poacher-ii-production-v2.json`, bundle ID
`424a5aaae9d0f6ca149a47c01374e0a18cd98991811d9c963724e6e4c1fe8a2c`.
The human review then accepted that exact WAV. Decision
`f3363a1199fac0e01c66d0dc03bfa33f9d08bb1e64995cdc1affc141963a213e`
made the source item and approved-only manifest entry terminal with audio
SHA-256
`051889ddc892a41758b1cad9b2970e3c27b7d5c04a22f1dd3b43f2a113ab9337`.

A broad reconciliation over every retained historical bundle exposed one
superseded specialist workspace that no longer passes the current
sentence-repair text validator. The merge therefore used contained immutable
scope `reconciliation-scopes/current-character-story-poacher-ii-approved-v1`
rather than weakening validation or ignoring the error. Reconciliation
`current-character-story-20260828-poacher-ii-approved-v1.json`, report ID
`107f7bbda32b4a280f5a8d1639e7f9865fa2f5a18fcc67c3f0d36be0996a8d46`,
contained exactly one `terminal_merge_required` action and no conflicts. Its
successor is
`resume-395a5e5eec0327a3a793b66d-b5f60046824c9d2d`: Poacher II is approved,
both Dobharchú production WAVs were still pending at that intermediate point,
every other state item was unchanged, and the approved-only manifest contained
410 entries.

The first Dobharchú reconciliation exposed a projector gap: the unique approved
source was present, but the newer primary's `generated/pending_review` actions
were not eligible for terminal projection. After extending the same exact
terminal gate to pending review actions and adding explicit repeatable
publication selection, real report
`current-character-story-20260828-dobharchu-534705-approved-v2.json`, report ID
`a57cc52effb3ae4cd5b6c39b54ba1f5b3966d19bc594cb457b89aae632db0485`,
was built directly against the retained full review-bundle directory while
opening only the named Dobharchú publication. It contains exactly two
`terminal_merge_required` actions and no conflicts.

`merge-reconciled-outcomes` published successor
`resume-395a5e5eec0327a3a793b66d-2624f64ea2773d6b`. Its state differs from the
Poacher successor in exactly the two Dobharchú queue IDs; both are approved
with the WAV SHA-256 values above. Poacher II remains approved with WAV SHA-256
`051889ddc892a41758b1cad9b2970e3c27b7d5c04a22f1dd3b43f2a113ab9337`,
all other item documents are unchanged, no generation is active, and the
approved-only manifest contains 412 entries.

## Aderyn and explicit-fallback composition checkpoint, 2026-08-28

Reconciliation
`current-character-story-20260828-aderyn-terminal-composition-v1.json`, report
ID `1713a4a2a84df9277a8aa86aca10cd7be8dc2a33ed236699aca179b029d6fee6`,
was built with `...-2624f64ea2773d6b` as primary and only the contained adult
and child Aderyn review bundles as secondary authority. It reported 83 exact
terminal merges, 21 generation-ready but unselected Aderyn lines, three
explicit-fallback actions and zero conflicts. The terminal merge successor is
`resume-395a5e5eec0327a3a793b66d-f3c1d02acde8308b`.

The three standalone Narrator decisions could not be passed through terminal
WAV reconciliation because an explicit live fallback intentionally has no WAV.
The dedicated `merge-explicit-fallbacks` boundary therefore composed only
these exact queue IDs from `...-a2b299862a4c4483`:

- `reverse1999:314606:43:09977e2b04515b66`;
- `reverse1999:314606:6:3511125b2e41a19f`;
- `reverse1999:314608:58:c3e23840e6ecc840`.

The immutable successor is
`resume-395a5e5eec0327a3a793b66d-2318390364c83cb8`. Its workspace SHA-256 is
`e123bde4199f0597c2b09e48963fea348959a1eac55b567ee18efeae340fe838` and
state SHA-256 is
`cd0630881f5c37a39de18092e2852c607154f3a3551a3e9732293f7248491b8e`.
It contains 471 approved WAVs, 90 rejected generated records, eight explicit
live fallbacks, zero failed records and no active generation. The eight
fallbacks are the five already composed Dobharchú decisions plus the three
named Narrator decisions. Repeating the exact merge returned the same
directory with `created=false`; the base and source state hashes remained
`9b320f972106f9ac46f33133cb7b6142a2de603da53c204eaa0ed82cbedb4ff6`
and `04a59cc5baf0884abe102489fda8a794db9bb334ac0f950631af440d21548434`.

This checkpoint still has 21 absent Aderyn speech items and seven typed
non-verbal events. It is not a publishable complete pack until those exact
identities receive supported terminal authorities.

### Routed Aderyn fallback checkpoint

The 20 Aderyn speech items without a completed candidate were then resolved by
the schema-v5 known-role live-fallback boundary. Every queue ID was paired with
its exact inactive failed source: five exhausted Pocket outcomes from
`...-8cb0f74eaca6cf10`, inline-pause failures from `...-80c3fbbc77a13741`
and `...-cdeaaf20a0f9849f`, and the 13 alternative-reference outcomes from
`...-40a7a5a24af271d0`, `...-706e025553318f5f`,
`...-cbecf377c313d430` and `...-d96c8e798db08e17`. The batch embeds every
failed item and binds its workspace/config/state/item hashes to the current
known-role manifest and combined queue-override digest.

The immutable successor is
`resume-395a5e5eec0327a3a793b66d-1caa7918f07cf59a`, with workspace SHA-256
`3477295cad98dedb7a086c1f7f5aec28c284a63fac5b0138cbbd28c6b858f3f2`
and state SHA-256
`1cc2db4e172a827a03fdd6bcc5ee60ee2a69c1fe1947490531a60f4d1efe0bc3`.
It has 471 approved WAVs, 88 rejected WAVs, 28 explicit live fallbacks, one
pending Aderyn WAV, zero failed records and no active generation. A real repeat
returned the same directory with `created=false`. Runtime validation loaded all
20 new decisions with `speaker=Aderyn` and requested Pocket voice `Rhiannon`;
none uses Narrator or Centurion.

Only `reverse1999:314606:65:1664f7ace785d5c3` remains as Aderyn speech work.
Its selected hypothesis must still cross the separate terminal audio-quality
gate because hypothesis preference is not approval.

### Pure audio-event omission checkpoint

The seven pure events with no installed semantically validated source and no
currently supported local effect generator were terminalized without WAVs or
TTS attempts. The exact IDs cover Aderyn `*whimper*` and `*yelp*`, Narrator
`*pop*`, the three `*bang*` records and `*buzzzzz*`. Every typed plan has an
empty spoken projection; mixed `gurgle` and `gasp` dialogue was deliberately
excluded.

The immutable successor is
`resume-395a5e5eec0327a3a793b66d-f122e31c31923351`, with workspace SHA-256
`fdd6cb4b345a156f9cb172709caea04b0f2d56ce82e57d51ef34cb3227a5d8d8`
and generation-state SHA-256
`9f421bdb9fdd89bea67f48ab5a54477d4f99fa35d65eb90d1625ad8d0fc9491f`.
It has seven `omitted/omitted` authorities, 471 approved WAVs, 28 effective
live-fallback decisions, zero failed records, no active attempt and no partial
WAV. Its recorded base-state hash exactly matches the unchanged predecessor
hash
`1cc2db4e172a827a03fdd6bcc5ee60ee2a69c1fe1947490531a60f4d1efe0bc3`.
Repeating the exact command returned the same directory with `created=false`,
and the runtime manifest validator loaded all seven omission identities.

The final Aderyn speech gate is prepared separately as one-sample review bundle
`current-character-story-aderyn-final-quality-v1.json`, bundle ID
`d18033000170a8f842576a686515e35e8084641e543f892462024be26108ff44`.
It contains only `reverse1999:314606:65:1664f7ace785d5c3`, has no technical
attention flags, and binds candidate workspace/state, the 4.72-second WAV and
SHA-256
`06a616bfe87ef15d250718e7b809e144ec84969e120582d2ee0d01b431bee871`.
Accepting or rejecting that cohort is the remaining human audio-quality action;
the bundle publication itself changes no generation state.

### Final Aderyn approval and composition

The operator approved the exact final Aderyn WAV through that one-sample
bundle. Decision
`93b47d32076d1fd051785c11ae2e0ce1c13a19e79d2c98c921e54e24a8c37b24`
binds the 4.72-second WAV with SHA-256
`06a616bfe87ef15d250718e7b809e144ec84969e120582d2ee0d01b431bee871`;
the source voice remains `Aderyn` and the synthesis voice is the explicit
`Rhiannon` binding.

Reconciliation
`current-character-story-20260828-aderyn-final-approved-v1.json`, report ID
`b0284b8d23b9693e0f1166323851fcb3c95900e3988ec4782f84b70fce9972b7`,
contained exactly one `terminal_merge_required` action and no conflict. Applying
it to the pure-event omission checkpoint produced immutable successor
`resume-395a5e5eec0327a3a793b66d-bc27ca69bfa40f01`, workspace SHA-256
`4036a21f733f4db8514254cc9e3d2ecff1aaffbf84bb1ea2580a049b6d76da51`
and generation-state SHA-256
`48fd111f847a7f10b4638938d41cf2ba486b84914fa6c658ec513742f28f1f57`.
All 592 queue identities are present: 472 are approved, seven are explicit pure
event omissions, 28 have effective live-fallback decisions, 88 rejected WAVs
remain historical terminal evidence, and there are zero pending or failed
records and no active generation. This is the composition baseline for the
mixed-event projection boundary and final pack publication.

### Mixed audio-event projection checkpoint

The two rejected mixed speech/event lines are now explicit schema-v6 Pocket
live fallbacks. Immutable successor
`resume-395a5e5eec0327a3a793b66d-d15c76db0b8ee933`, workspace SHA-256
`80a586679b048f1dc305e071050b15f16c6ef672e051e32b40e68c541cda6b35`
and generation-state SHA-256
`f16373fd0a9eaee79cf65f1470fd208737474257e7d9d1f926c04c0e7942b76f`,
retains both rejected WAVs as embedded checksum-bound evidence. Runtime routes
`reverse1999:314607:83:327de590ff262fcc` as `Wh-What!` and
`reverse1999:314607:84:e1ab45a7b54e20d4` as `N-No!` through Pocket's
`Narrator` voice; neither `*gasp*` nor `*gurgle*` reaches a speech backend. Batch
ID is
`c311430011502fee757dd3e32844768c4417c67587d0b3a59fb58ca426aa80ed`.
An exact repeat returned the same directory with `created=false`.

The first final-pack attempt then failed closed before creating its destination:
the composed state has no top-level `synthesis_controls` registry. Of its 472
approved WAVs, 430 retain an item-level synthesis-provenance hash and 42 are
older reviewed `provider=vntts` records from before per-control inventory was
introduced. Five additional old unbound WAVs are rejected and are not pack
payload. No historical control set may be invented from the current manifest;
publication requires an explicit reviewed-waveform migration authority instead.

### Reviewed-waveform migration and portable pack checkpoint

The exact migration successor is
`resume-395a5e5eec0327a3a793b66d-c5a8b5a17cd5fce6`, workspace SHA-256
`f88c3384848b776752b215671537f3c6e3b036f6f62516c59d591e73661360de`
and generation-state SHA-256
`d348d3f4fbc3fc271826df9860e7b2fa5097778348f35d62b87cc196a2777ba7`.
Batch
`5379f66a8f256f9820802ec0125d589b234ce782e539ffe402600781120fb732`
binds all 472 approved queue/item/WAV identities to their unchanged base
results. It preserves 409 active config-rebase routes and labels the 63 older
records as historical reviewed waveforms with no reproducibility claim. An
exact repeat returned the same directory with `created=false`; predecessor
workspace and state hashes remained
`80a586679b048f1dc305e071050b15f16c6ef672e051e32b40e68c541cda6b35`
and
`f16373fd0a9eaee79cf65f1470fd208737474257e7d9d1f926c04c0e7942b76f`.

Portable checkpoint pack
`authoring/game-packs/current-character-story-3.7-v1` has game-pack SHA-256
`587d7bdc3c38cbf39a6bb22c6ebb3f9f5b3b8777eab10c15d3a96f5443c374a3`
and passed `vntts-preflight-game-pack`. It contains 472 approved WAVs, 30
explicit live-fallback decisions and seven omissions. The three configured
Centurion OGG references were decoded only in staging to PCM16 mono WAV at
24 kHz; the projection ledger binds each source/output hash and the original
workspace references remain unchanged. Runtime metadata selects Centurion and
routes the two mixed events as exact spoken projections `Wh-What!` and `N-No!`,
without the literal `*gasp*` or `*gurgle*` markers.

This v1 is a verified checkpoint, not the final all-lines acceptance candidate:
83 rejected historical/generated records still rely on ordinary live routing
rather than an explicit checksum-bound fallback decision. Their exact route
distribution is 24 Aderyn/Rhiannon, 22 Narrator/Centurion, 14 Rhiannon, seven
unknown/Centurion, six Dobharchú, four Poachers, five legacy Rhiannon records
whose item speaker field predates normalization, and one Aderyn's Father. The
next immutable successor must bind those already rejected results to their
current effective live characters before publishing a v2 pack.
