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

The published real-data plan ID is
`f441a8f9366f7f3c61892802f5b36bd8d19ffe97e036e2a6e2b1393a2fe477be`
and its file SHA-256 is
`41eefae7c4a3dea4a8aa578ccf48c22718b3b63d26dbbe4fe032fb4f79854b89`.
The human decision ID is
`f4c7ebfe5bec30b7649e12b6b75b853221ddffcf4660ff83d7a4f2cb302056f1`,
its file SHA-256 is
`c186caae367b598ce48901fdd64e17cd9d67444416850fe2589dbc938c9ef226`,
and both variants map to logical identity
`cc0370b5c6aa05bce10c30bcef36daa9a0468e34fd400e2486b6e51201d64f51`.
