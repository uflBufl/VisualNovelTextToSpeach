# Single-candidate render hypothesis review

An alternative-reference comparison can produce only one complete render for an
exact queue item. That result is useful evidence, but it cannot enter a blind
A/B session because the other arm has no WAV. The render-hypothesis review
preserves this boundary without rerendering or changing generation state.

`render-hypothesis-review-publish` copies the exact comparison document, selected
arm report, source reference and generated result into a no-replace directory.
The review ID binds the comparison, arm, queue/text identity, reference and
result SHA-256, model/profile and seed. The reference retains its real media
format; the generated result must remain a non-empty PCM16 mono WAV. Loading or
deciding revalidates every copied byte and rejects traversal, symlinks, stale
hashes, incomplete arms and ambiguous reference controls.

The only decisions are:

- `accept_hypothesis`: this exact reference/result pair sounds suitable for one
  subsequent production-shaped hypothesis;
- `need_different`: do not reuse this pair.

Neither decision approves a story line, edits generation state, publishes a
manifest or establishes a speaker-wide reference preference. A later
production-shaped render remains a new checksum-bound artifact requiring its
normal individual review.

```bash
uv run vntts-pregenerate render-hypothesis-review-status REVIEW_DIRECTORY
uv run vntts-pregenerate render-hypothesis-review-decide \
  REVIEW_DIRECTORY accept_hypothesis
```

Use `need_different` instead of `accept_hypothesis` when the result has wrong
words, identity, pacing, pauses or artifacts. Repeating the same decision is
idempotent; changing a terminal decision is rejected.

## Current Character Story evidence

Two reviews were published on 2026-08-27 from immutable comparison
`current-character-story-narrator-alternative-reference-v2`, without a model
run or generation-state write:

- `current-character-story-narrator-314608-58-v1`, review ID
  `51600bc4f16270096f6bced0e23ee2d14f2a0c0b6eb1724da84f039810a62834`,
  binds Centurion reference 02 SHA-256 `d2be6ca0eedf...` and result SHA-256
  `7ff11a602d9a...`;
- `current-character-story-narrator-314608-94-v1`, review ID
  `3788865d35278c9c8b0a04512278dbc193b1a3107755ece3cbd9c5fdf99f8e60`,
  binds Centurion reference 03 SHA-256 `f14f99ac31eb...` and result SHA-256
  `e6af9b24e2cc...`.

Both decisions remain unset. They require one explicit human verdict each.
