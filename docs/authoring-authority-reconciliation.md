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

The dependency direction is intentionally one-way: reconciliation consumes the
shared public `authoring.authority` snapshot/hash/no-replace primitives and the
public captured-document validators from cohort, quality, generation-state and
workbench modules. The authority module depends only on the standard library;
it never imports a UI, workspace or synthesis implementation.

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
- `generation_ready_unselected`: immutable controls are ready, but no exact
  generation selection was authorized;
- `workspace_blocked`: workspace controls fail their current readiness gate.

Approved, rejected and explicit-fallback results are terminal counts, not new
actions. Terminal decisions that disagree across parallel workspace histories
are reported as conflicts; reconciliation never chooses one history as the
merge winner.

## Current Character Story evidence

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

It remains readable for provenance, but it must not be treated as the final
merge claim until rebuilt with the original-publication scope. Any report is
planning evidence only: it does not authorize the final manifest until terminal
decisions and supported fallbacks cover the selected game pack.
