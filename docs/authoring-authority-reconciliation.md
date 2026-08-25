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
only items in that bundle's current successor. Completed bundle sources remain
in the workspace ledger for provenance but do not reopen terminal work. Older
bundle schemas and unrelated JSON documents are ignored. A queue item appearing
under two distinct current bundle authorities fails the run rather than being
merged by filename or chronology.

Source-quality reviews are explicit arguments because the application-data root
may retain old, superseded, but still internally valid pending sessions. Folder
presence and an old `completed_count` cannot establish current product scope.

Every workspace is loaded by the authoring workbench validation path. Queue,
state, generated manifest and every reported pending WAV are hash-bound; current
bundles and quality reviews also run their full public validators. All observed
files are rehashed once more before the report ID is calculated. A concurrent
change fails the run. The output is published with no-replace semantics.

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

The 2026-08-25 run used merged workspace
`resume-395a5e5eec0327a3a793b66d-cd54b7632c220de2`, all seven current v2 bundle
publications, and exactly the Mrs. Owen and Hotelier quality cards. The final
report is
`authoring/reconciliations/current-character-story-20260825-04d09414e72e.json`:

- report ID:
  `04d09414e72e2b69d6e4cb01c93ec03716d7d2ee0137f16de8e2117ed95a2726`;
- file SHA-256:
  `6001eb61656b88a915c1e3a7ba93a78e63351fc39d4a1a0c7ba535620b18c014`;
- 28 provenance workspaces and seven current bundle publications validated;
- zero conflicting terminal authorities;
- 25 exact current cohort-review items;
- two explicitly current source-quality decisions, Mrs. Owen and Hotelier;
- 129 primary pending WAVs requiring a risk-based review plan, not listen-all;
- 15 primary failures requiring a new bounded hypothesis;
- 164 primary lines requiring an exact source reference or explicit fallback.

The report is planning evidence only. It does not authorize the final manifest
until terminal decisions and supported fallbacks cover the selected game pack.
