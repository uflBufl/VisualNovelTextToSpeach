# Portrait expression aliases

Story producers must preserve the exact portrait ID attached to each line. Two
IDs can nevertheless show the same person with a different facial expression.
VNTTS treats this as a review and coverage alias, not as permission to rewrite
the source record or silently pool voice references.

`portrait-alias-plan` reads a completed, checksum-validated source-reference
quality review and proposes pairs only when all of these conditions hold:

- both source-reference variants were explicitly accepted;
- both have checksum-bound portrait PNGs;
- normalized character names and source-bank identities are equal;
- portrait IDs differ; and
- the 64-bit difference-hash distance is within the bounded threshold.

Visual similarity is only a suggestion. It never creates synthesis authority.
`portrait-alias-decision` requires an explicit accepted suggestion ID and emits
an immutable identity containing both original variant IDs, portrait IDs,
portrait hashes, dHashes, character and source bank. Downstream tools may group
review or coverage by that identity, but must continue recording the exact
variant and reference WAV used for each synthesis result.

Different characters, source banks, ages or life stages are hard boundaries.
Missing portraits and variants without accepted source-reference decisions are
not guessed. A visually similar but unbound portrait remains unresolved until
source evidence or a separate explicit human decision exists.

The commands are:

```bash
uv run vntts-pregenerate portrait-alias-plan REVIEW.json \
  --max-dhash-distance 6 \
  --output ALIAS-PLAN.json

uv run vntts-pregenerate portrait-alias-decision ALIAS-PLAN.json \
  --accept-suggestion SUGGESTION_ID \
  --output ALIAS-DECISION.json
```

Both publications are no-replace. Loading a plan revalidates the complete
source review, exact portrait bytes and deterministic similarity result.

## Dobharchú decision

The Character Story quality review produced exactly one suggestion at distance
3: portraits `534703` and `534704`, both Dobharchú variants from
`activityvoc_story_npcnoname323_beiai.bnk`. The user confirmed that these show
the same person with different expressions. The decision preserves variant IDs
`cluster-2f4d52a49d13c24bbd0e74ad-anchor-1` and
`cluster-e8dcae5254441ab7633ba7d9-anchor-1` under one logical identity. Portrait
`534705` remains outside the decision because no checksum-bound portrait or
source-audio route exists for it.
