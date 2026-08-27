# Authoring authority reconciliation

`vntts-authoring-reconcile` publishes a deterministic read-only handoff over one
explicit primary workspace, the current checksum-bound cohort bundles in one
contained bundle directory, and only the source-quality reviews named by the
operator. It never applies a decision, changes state, rebuilds a manifest,
starts generation or infers approval from a WAV file.

```bash
uv run --no-sync vntts-authoring-reconcile \
  --primary-workspace /authoring/workspaces/resume-... \
  --bundle-root /authoring/review-bundles \
  --quality-review /authoring/source-reference-quality-reviews/current-a/review.json \
  --quality-review /authoring/source-reference-quality-reviews/current-b/review.json \
  --output /new/reconciliation.json
```

The primary workspace contributes its complete spoken generation inventory.
Other workspaces are admitted only through a current v2 bundle and contribute
the immutable item inventory of the original publication, not merely the
remaining successor. A completed cohort disappears from the operator's current
sample list, but its exact source item remains in reconciliation scope so a
later approved, rejected or explicit-fallback authority cannot be lost. Older
bundle schemas and unrelated JSON documents are ignored. A queue ID appearing
with different full queue-record hashes or conflicting terminal authorities is
reported as a conflict rather than merged by filename or chronology.

Source-quality reviews are explicit arguments because the application-data root
may retain old, superseded, but still internally valid pending sessions. Folder
presence and an old `completed_count` cannot establish current product scope.

Every workspace is loaded by the authoring workbench validation path. Queue and
state semantics are parsed from captured bytes, while workspace, selected voice
manifest, voice controls, generated manifest and every in-scope generated WAV
are hash-bound. Cohort publications, mutable progress and quality reviews are
also parsed from captured documents through their public validators. Missing
state, manifest and progress paths are bound as absences, and the bundle
directory inventory is checked again. All observed files are rehashed before
the report ID is calculated; symlink substitution or a concurrent appearance,
removal or byte change fails the run. The output uses shared no-replace
publication semantics.

The public report reader validates the complete version-1 wire document:
required top-level and nested fields, canonical paths and identities, enums,
lowercase SHA-256 values, unique workspace/action/conflict identities and every
summary/count equation. Recomputing `report_id` does not make an incomplete or
internally inconsistent document valid. Historical version-1 documents using
the older `current_bundle_items_only` scope remain readable, but new reports
emit `original_bundle_items_only` and should replace them for final merge
planning.

The dependency direction is intentionally one-way. The versioned wire schema,
nested record validation and count equations live in
`authoring.reconciliation_schema`; that module imports only the shared
`authoring.authority` canonical-document helper. The reconciliation projector
consumes that schema plus the public captured-document validators from cohort,
quality, generation-state and workbench modules. The authority module depends
only on the standard library; it never imports a UI, workspace or synthesis
implementation.

An AST import-graph audit after the split found no strongly connected component
among authoring modules. Follow-up slices removed the listening/listening-UI and
legacy/listening-import components, separated terminal workspace application
into a leaf orchestration module and kept the former direct `workbench` merge
import as a lazy compatibility facade. That facade therefore preserves callers
without restoring a static terminal-workflow import cycle.

The item-level next actions are deliberately conservative:

- `human_cohort_review`: an exact current bundle already defines the samples
  and decision boundary;
- `review_plan_required`: a pending WAV exists, but a risk-based cohort plan
  must select technical-attention items and deterministic clean controls before
  asking a person to listen; it does not mean listen to every item;
- `human_source_quality_review`: one explicitly selected reference-quality card
  needs a perceptual decision;
- `new_hypothesis_required`: generation failed and another blind seed or larger
  limit is not authorized;
- `source_reference_or_explicit_fallback`: the selected workspace has no usable
  voice for that line;
- `terminal_merge_required`: the primary item is nonterminal, but exactly one
  current secondary workspace already owns a checksum-bound approved, rejected
  or explicit-fallback outcome; merge that named state item instead of
  synthesizing again;
- `generation_ready_unselected`: immutable controls are ready, but no exact
  generation selection was authorized;
- `workspace_blocked`: workspace controls fail the readiness gate for the exact
  otherwise-covered absent queue-ID selection.

The last two states are deliberately selection-aware. A partial voice manifest
can make an unfiltered workspace report `NEEDS_ATTENTION` because some other
speaker is unresolved while still allowing an exact covered queue-ID batch.
Reconciliation projects all absent, voice-covered IDs through the same
`inspect_generation_readiness(..., queue_ids=...)` contract used by
`generation_command`; global missing-voice reasons never relabel those covered
items as blocked. This remains planning evidence only: the report does not
launch the exact batch or widen it to the missing-voice IDs.

Approved, rejected and explicit-fallback results are terminal counts, not new
actions. Terminal decisions that disagree across parallel workspace histories
are reported as conflicts; reconciliation never chooses one history as the
merge winner.

## Current Character Story evidence

The original-publication-scope rebuild from clean commit `7ce861d` is
`authoring/reconciliations/current-character-story-20260825-8e49fc5171f4.json`:

- report ID:
  `8e49fc5171f4f1fd5bc07bc8c802d05f7126a8701654e24d7e1cf9d6a5836bbe`;
- file SHA-256:
  `60bf25a3d163c534392947203ab3995439872593ede53b8a7a876b36a2379b16`;
- 28 provenance workspaces, all seven current v2 bundle publications, and both
  explicitly selected source-quality reviews validated from captured bytes;
- 25 exact current cohort-review items, 129 primary pending WAVs requiring a
  risk-based plan, 15 bounded-hypothesis failures, and 164 lines requiring an
  exact reference or explicit fallback;
- five terminal conflicts were recovered from completed original bundle
  inventories: `314602:92`, `314602:110`, `314608:27`, `314608:35`, and
  `314608:71`. Each conflict has identical queue-record and text identity but
  contains an older rejection and one or more newer approvals. The report does
  not choose among them; an explicit human authority is required.

The report was first built under `/private/tmp`, loaded through the public
strict reader, and then republished byte-for-byte through the public no-replace
writer. No workspace, bundle, progress file, quality review, state, manifest,
or generated WAV was changed.

### Historical current-successor report

The original 2026-08-25 run used merged workspace
`resume-395a5e5eec0327a3a793b66d-cd54b7632c220de2`, all seven current v2 bundle
publications, and exactly the Mrs. Owen and Hotelier quality cards. That
historical report is
`authoring/reconciliations/current-character-story-20260825-04d09414e72e.json`:

- report ID:
  `04d09414e72e2b69d6e4cb01c93ec03716d7d2ee0137f16de8e2117ed95a2726`;
- file SHA-256:
  `6001eb61656b88a915c1e3a7ba93a78e63351fc39d4a1a0c7ba535620b18c014`;
- 28 provenance workspaces and seven current bundle publications validated;
- zero conflicts under the historical current-successor scope;
- 25 exact current cohort-review items;
- two explicitly current source-quality decisions, Mrs. Owen and Hotelier;
- 129 primary pending WAVs requiring a risk-based review plan, not listen-all;
- 15 primary failures requiring a new bounded hypothesis;
- 164 primary lines requiring an exact source reference or explicit fallback.

It remains readable for provenance, but its zero-conflict statement applies
only to the historical current-successor scope and must not be used as the
final merge claim. Any report is planning evidence only: it does not authorize
the final manifest until terminal decisions and supported fallbacks cover the
selected game pack.

### Selection-aware v6 report

After retiring the unsafe child-Aderyn reference and carrying the five
unchanged terminal decisions, clean commit `a8da063` published the current
report as
`authoring/reconciliations/current-character-story-20260827-d29530a87b6b.json`:

- report ID
  `d29530a87b6b11d9a2a29815c18604fffedda0933405b9ecc9a1fd4c1612420d`;
- file SHA-256
  `8dc6b4769cc124e08336dbaab569a35eafecd11f96889d8d53904fbc76b5c51f`;
- 29 workspaces, eight cohort bundles and the exact Mrs. Owen and Hotelier
  quality reviews;
- 49 `generation_ready_unselected`, 15 `human_cohort_review`, 12
  `new_hypothesis_required`, 118
  `source_reference_or_explicit_fallback`, zero `workspace_blocked`, and six
  terminal conflicts.

The 49 exact ready IDs comprise 34 Mrs. Owen, 12 Hotelier, two Dobharchú and
one Aderyn line. A child-command preflight over precisely that tuple reported
`selected=49`, `pending=49`, `ready=49`, `missing_voice=0` and no blockers;
the derived argv contained exactly 49 `--queue-id` arguments with `retries=0`
and `seed=0`. No child was launched because the remaining Dobharchú cohort and
new terminal-conflict case are still human authority gates. The report build
rehash-validated every captured source and did not mutate a workspace, state,
manifest, bundle, progress file or WAV.

That report is retained as evidence of the selection-readiness correction, but
its next-action projection was superseded after reconciliation learned to
surface already reviewed secondary outcomes. Clean commit `b5d8173` published
`authoring/reconciliations/current-character-story-20260827-3ad299f22074.json`:

- report ID
  `3ad299f2207451dff9e7293f2e28023133d5cf5fc99e0ba5b0b5092f27760ea5`;
- file SHA-256
  `6e77b8e55a4d2d14685f7de3ab38c1048f9c4065930ac88279987bb65b8e73c3`;
- 47 `generation_ready_unselected`, nine `terminal_merge_required`, 15
  `human_cohort_review`, five `new_hypothesis_required`, 118
  `source_reference_or_explicit_fallback`, zero `workspace_blocked`, and the
  same six terminal conflicts.

The nine merge actions bind six approved and three rejected exact state items.
Seven are the completed Pocket fallback outcomes; the other two prevent one
already approved Dobharchú result and one already rejected Aderyn result from
being generated again. Every recorded source state-item SHA-256 was recomputed
from its named current workspace and matched. The resulting exact pending
selection contains 34 Mrs. Owen, 12 Hotelier and one Dobharchú line. Its child
preflight reported `selected=47`, `pending=47`, `ready=47`,
`missing_voice=0`, no blockers and exactly 47 `--queue-id` arguments. No merge
or generation ran: both remain downstream of the two outstanding human review
authorities.

The reusable apply boundary is now
`vntts-pregenerate merge-reconciled-outcomes PRIMARY_WORKSPACE REPORT`. It
accepts only `terminal_merge_required` actions for the report's exact primary
workspace and copies only their named approved/rejected state items. The
successor's schema-v2 outcome ledger binds the reconciliation ID and every
source state/item/WAV digest. Source workspaces can contain other terminal
items without making them eligible. Primary or source config, queue, state,
item, authority or WAV drift fails before atomic no-replace publication, and
the same report is idempotent. The real report above has not been applied: its
nine-item merge was deliberately gated on the unfinished Dobharchú cohort and
the sixth terminal-conflict decision and is now historical evidence only.

### Final post-review Character Story authority

The Dobharchú cohort and terminal conflicts are now terminal. A fresh
reconciliation over composed workspace
`resume-395a5e5eec0327a3a793b66d-0f0300f2c7b702ad` is published as
`authoring/reconciliations/current-character-story-20260827-d3da2f94cc94.json`:

- report ID
  `d3da2f94cc945da2a1af5a3a7ae643744ef3377fcf837f032589972a804ea700`;
- file SHA-256
  `11cbdd8e305e4e7049664582a4b14b21b9e5f4efa85f95c2b906a6e0307bbb3b`;
- zero terminal conflicts and zero `terminal_merge_required` actions;
- 46 exact `generation_ready_unselected`, five
  `new_hypothesis_required`, and 118
  `source_reference_or_explicit_fallback` actions.

The primary state has 345 approved, 71 rejected, five failed and 46 pending
items. Its workspace-document SHA-256 is
`7f4fd53ad7e2051ac7c0af21264a04376c89ddfe72892417319aead3677329b2`.
Applying an older report again or using its superseded primary workspace is no
longer part of the completion path.

## Lease and terminal-publication invariants

Generation, publication and terminal-review progress use a persistent
cross-process advisory guard around lease creation, stale recovery and cleanup.
The guard file is never renamed or deleted, so every process locks the same
inode. A process may archive a stale lease only while it owns that guard and
only if the exact lease bytes still match the snapshot it classified as stale.
Acquisition is non-blocking and reports the current owner; cleanup waits for a
short concurrent transition so a committed operation cannot leave its own
still-live lease behind. PID, host and process-start identity remain the
operator-readable ownership record, while the advisory guard supplies the
compare-and-swap boundary.

Any generation-state item carrying `terminal_conflict_resolution` is
publishable only from its canonical config-addressed workspace. Both the
approved-only generated manifest and final game-pack publisher load and
validate that complete workspace, bind the exact canonical queue/state bytes,
and require the item-level provenance to equal the workspace merge ledger.
An orphaned state, a shape-valid fabricated record, or a minimal replacement
`workspace.json` is rejected before manifest entries or a destination pack are
published. The terminal review wire format and supported UI deliberately
require exactly two distinct candidates; no-replace filesystem errors are
translated at each review, resolution and successor domain boundary.
