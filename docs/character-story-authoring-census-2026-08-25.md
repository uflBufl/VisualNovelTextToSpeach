# Character Story authoring census, 2026-08-25

This is a read-only reconciliation of the current checksum-bound application
data for `The You That's Meant To Be`. Paths below use workspace and bundle
identities rather than a user-specific application-data prefix. The census is
planning evidence; it does not authorize generation, review, approval or final
publication.

## Aderyn branch update, 2026-08-28

The exact child and adult Rhiannon-routed Aderyn reviews are terminal. Their 83
new WAV decisions are 59 approved and 24 rejected. They were merged with the
nine prior Aderyn approvals into config-addressed successor
`resume-395a5e5eec0327a3a793b66d-af9b4fb0bb4a451c`, state SHA-256
`a292099ecb6a8b5b46f472e66482fcdd8e9046246cc49482c909749d98b43704`.
The Aderyn slice now has 68 approved, 24 rejected and 21 failed records. The
whole successor has 468 approved, 88 rejected, 24 failed, one pending, five
live fallbacks and two missing-voice lines.

The 21 remaining Aderyn failures retain source-specific attempt authority:
five are exhausted Pocket failures, three are inline-pause comparisons and 13
are alternative-reference comparisons. The accepted Rhiannon reference is 01;
02 and 03 produced large pauses and are not authorized as a replacement batch.
The exact source-workspace partition is recorded in
[`current-character-story-completion.md`](current-character-story-completion.md).
Do not use the merged successor's generic repair projection to spend another
seed: terminal-outcome merge does not overlay nonterminal branch history.

## Authoritative merged history

The current merged workspace is
`resume-395a5e5eec0327a3a793b66d-cd54b7632c220de2`:

- queue SHA-256:
  `1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`;
- state SHA-256:
  `c673b8631045c0d2a6206c6458f93b38b4b39e9b30b8efd3acd5ebbd893c2cf6`;
- 421 authoritative state records: 197 approved, 129 generated/pending,
  80 generated/rejected and 15 failed;
- 406 WAVs and an approved-only 197-entry manifest with SHA-256
  `68ae729131893de5a0f0e08450c478757f519e54a749b36b421f0736485a7983`;
- no active attempt.

The 15 failures still classify exactly as four sentence-boundary candidates,
one bounded-seed candidate, seven exhausted-primary Pocket candidates and
three direct reference comparisons. Those labels describe the old merged
authority. The separately executed successors below supersede the permitted
experiments without mutating it.

## Current review publications

| Publication | Root bundle | Current progress | Current action |
| --- | --- | --- | --- |
| `current-character-story-specialists-v2.json` | `f9131e03...` | 18/18 cohorts complete, 0 samples left | none |
| `current-character-story-specialist-followups-v1.json` | `73b59932...` | 9/9 cohorts complete, 0 samples left | none |
| `current-character-story-selected-reference-repairs-v1.json` | `419a2118...` | 8/8 cohorts complete, 0 samples left | none |
| `current-character-story-final-pocket-fallbacks-v1.json` | `692a8aa0...` | 3/3 cohorts complete, 0 samples left | retain as completed evidence |
| `current-character-story-dobharchu-natural-expansion-v1.json` | `d0f42e5e...` | 2/4 cohorts complete, 17 exact samples/items left; current bundle `4fdab0cd...` | human review remains required |
| `current-character-story-exhausted-primary-pocket-fallbacks-v1.json` | `3cf27ce5...` | 0/3 cohorts complete, 7 exact samples/items left | human review remains required |
| `current-character-story-rhiannon-inline-pocket-fallback-v1.json` | `3b26c781...` | 0/1 cohort complete, 1 exact sample/item left; no progress checkpoint | human review remains required |
| `current-character-story-primary-pending-risk-review-v1.json` | `3760c1ba...` | 3/3 cohorts complete, 0 samples left; all 129 exact WAVs accepted | retain as completed evidence |

Older JSON documents rejected by the current cohort-bundle loader are legacy
plans, decisions or superseded bundle schemas. Their presence does not reopen a
completed current publication and must not be treated as a current review
count.

## Executed repair successors

| Workspace suffix | Exact result | State SHA-256 | Next action |
| --- | --- | --- | --- |
| `25dc94e1e521dab8` | five selected MOSS repairs all failed; no new WAV | `3d8dbe13...` | alternative-reference comparison for the four failed segmentations; separate inline-pause path for the fifth |
| `dee61c5ea3baf68c` | seven selected Pocket fallbacks produced seven pending WAVs | `f18053ef...` | finish its three-cohort checksum-bound bundle only |
| `c6dee25b528e1487` | Rhiannon 180 ms inline-marker MOSS attempt failed at the bounded audio limit; no WAV | `0f48987a...` | no more MOSS seed or limit increase |
| `a2c30805e8846457` | one unseeded Rhiannon Pocket fallback produced one pending WAV | `3d517d1f...` | finish its one-sample checksum-bound bundle only |
| `b3a3c14c9725777a` | Dobharchu successor currently has four approved, 214 generated and 153 failed records | `663dd1b7...` | finish only the current remaining exact cohort review and bounded comparisons |
| `3e8158ddf2fdb81a` | ten Dobharchu sentence repairs produced seven rejected WAVs and three failures | `e2ec1df6...` | preserve terminal rejects; compare remaining exact failures under new controls only |

All listed workspaces had `active=null` during the reconciliation. The source
merged workspace and its approved-only manifest were not changed.

## Remaining comparison-only records

After accounting for the executed successors, seven failures had an
evidence-backed alternative-reference question rather than permission for
another seed with the same controls:

| Source role | Queue ID | Reason |
| --- | --- | --- |
| Narrator | `reverse1999:314606:54:0450c81c4d1b3cc4` | direct internal-silence reference comparison |
| Narrator | `reverse1999:314608:58:c3e23840e6ecc840` | direct internal-silence reference comparison |
| Narrator | `reverse1999:314608:94:f6c23264391ffae3` | direct internal-silence reference comparison |
| Narrator | `reverse1999:314606:43:09977e2b04515b66` | bounded sentence repair still failed |
| Narrator | `reverse1999:314606:6:3511125b2e41a19f` | bounded sentence repair still failed |
| Dobharchu | `reverse1999:314602:103:d579ac2a70771e37` | bounded sentence repair still failed with the short portrait reference |
| Dobharchu | `reverse1999:314605:109:a5c710ca1debbf26` | bounded sentence repair still failed with the long portrait reference |

The bounded comparison has now been executed. Exact controls, outcomes and the
one complete blind pair are recorded in
[`alternative-reference-comparison-2026-08-25.md`](alternative-reference-comparison-2026-08-25.md).
Only `314606:54` produced a matched pair; two other Narrator lines produced one
unmatched complete arm, while both arms were typed limited for the remaining
two Narrator and both Dobharchu lines. No state or decision changed.

## Evidence-blocked roles

The exhaustive local bank audit is recorded in
[`missing-character-reference-audit-2026-08-25.md`](missing-character-reference-audit-2026-08-25.md).
Mrs. Owen's 3.172-second exact-bank candidate, media `562400954`, and its
generated fixed-corpus sample were accepted. They now bind 34 exact queue IDs
through the immutable v5 multi-plan manifest documented in
[`pregeneration-coverage-plan.md`](pregeneration-coverage-plan.md). Hotelier's
exact bank has only five short clips and no technical pass; its terminal
decision is `needs_sample`, and its 12 lines use only an explicit Hotelier-only
Narrator fallback in the new preflight workspace. Its reused portrait does not
broaden identity.

## Completion boundary

The remaining Character Story authoring work is therefore not another broad
generation pass. It is:

1. finish the one remaining 15-item Dobharchu cohort decision;
2. publish and review the matched long-pause repair comparison after a suitable
   raw long-line capture exists;
3. merge only terminal decisions, rebuild the approved-only manifest and run
   the real routing/auto-advance acceptance.

The intervening config rebase is complete: successor
`resume-395a5e5eec0327a3a793b66d-7593a7c03fe36bc3` retains 324 approvals and 82
rejections while moving the remaining nonterminal work under the v5 Mrs. Owen
plus Hotelier-fallback configuration. Exact hashes and the no-generation gate
are recorded in
[`pregeneration-coverage-plan.md`](pregeneration-coverage-plan.md).

## Machine-verified reconciliation

The reusable read-only reconciliation contract and exact command are documented
in [`authoring-authority-reconciliation.md`](authoring-authority-reconciliation.md).
The original-scope 2026-08-25 successor report validates all seven current v2 bundle publications,
their 28 provenance workspaces, the primary merged queue/state/manifest, every
reported pending WAV, and exactly the current Mrs. Owen and Hotelier quality
cards. It preserves five exact cross-workspace conflicts where an older
rejected WAV and a newer approved WAV share the same queue-record/text identity;
those conflicts require the bounded review described in
[`terminal-conflict-review.md`](terminal-conflict-review.md). Its current actionable
projection is 25 exact cohort items, two source-quality decisions, 129 pending
primary WAVs needing a risk-based review plan, 15 failures requiring a new
hypothesis and 164 missing-voice lines requiring a source or explicit fallback.
No app-data decision, state, WAV, bundle progress or manifest was changed.

## Read-only verification and Narrator risk plan, 2026-08-26

A second full public reconciliation rebuilt the same report ID
`8e49fc5171f4f1fd5bc07bc8c802d05f7126a8701654e24d7e1cf9d6a5836bbe`.
The primary state remains exactly 197 approved, 129 generated/pending, 80
generated/rejected and 15 failed; its approved-only manifest remains 197
entries. The action projection is unchanged: 25 cohort-review items, two
source-quality decisions, 129 primary pending WAVs, 15 new-hypothesis failures,
164 missing-reference/fallback lines and five terminal conflicts. The terminal
review still has zero of five decisions, so no resolution, successor or merge
is authorized.

The ordinary primary cohort planner produces three exact Narrator cohorts by
seed: 88 seed-0 WAVs, 25 seed-1 WAVs and 16 seed-2 WAVs. Across them it selects
all 27 technical-attention WAVs and nine deterministic clean controls, for 36
required samples total and zero blocked items. An earlier accepted Narrator
cohort has the same reusable controls once the synthetic `Narrator` role is
resolved through the workspace's explicit `narrator_character=Centurion`:
provider `moss-tts`, stable profile, the exact local model digest, speaker
`reverse-1999-centurion-v2`, three ordered reference hashes, unapplied empty
prompt and `short-trailing-ellipsis-v1` transform all match for seeds 0, 1 and
2. The no-replace gate is
`current-character-story-centurion-voice-quality-gate-v1.json`, gate ID
`09720aa2f8fb10d0ac25c081bcdae1945deefa34190d96ad871ad40e6da236f5`.
It proves only control compatibility and cannot approve later WAVs.

The corresponding no-replace review publication is
`current-character-story-primary-pending-risk-review-v1.json`, root bundle ID
`3760c1ba0b251baf2f60d2cd30557f461cca45487b7aca4a1942124af8047205`.
It starts at 0/3 cohorts, 36/36 samples remaining and no progress checkpoint.
Publishing the gate and bundle did not run generation, apply any review
decision, change state/WAV/manifest authority or publish a final pack.

## Completed primary Narrator review, 2026-08-27

The current checksum-bound resume loader reports all three Centurion/MOSS/stable
cohorts complete and zero samples or pending items left. Their 129 exact WAVs
were accepted through the published bundle. The source workspace now contains
326 approved items, 80 explicit rejections and 15 failures; its approved-only
manifest contains exactly 326 entries. The authoritative state SHA-256 is
`2cdd8a18b4826f423bad7e06b719b07cd6b6a83e4bbd6ec38cf5e93893407f4e`.

The original version-1 plan remains immutable. Review-attention policy version
2 reduces its 24 silence/pause advisory selections to zero when projected
read-only from the same measurements, while leaving the strict synthesis gate
unchanged. The calibration and compatibility rules are recorded in
[`review-attention-silence-policy.md`](review-attention-silence-policy.md).

The completed approvals made the original terminal-conflict v1 source authority
stale without changing its ten candidate WAVs. The refreshed reconciliation is
`current-character-story-20260827-091d56596664.json`; its current terminal review
directory is `current-character-story-terminal-conflicts-v2`, with the same five
cases, zero initial decisions and current primary state authority. Details and
the fail-closed recovery are recorded in
[`terminal-conflict-review.md`](terminal-conflict-review.md).

All five refreshed cases were subsequently heard and selected. The immutable
resolution and successor retain three approved and two explicitly rejected
authorities, with no `neither` result. They were merged into config-addressed
workspace `resume-395a5e5eec0327a3a793b66d-63324a22121bb35e`; it contains 324
approved items, 82 explicit rejections, 15 failures and 164 missing-reference
lines. Exactly the five conflicted state records differ from the 326-approved
primary authority, and its derived manifest contains the 324 approved outcomes
only. The original primary, all source workspaces and both review bundles
remain immutable. Exact document identities and SHA-256 values are recorded in
[`terminal-conflict-review.md`](terminal-conflict-review.md).
