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

An accepted hypothesis can be imported into a newly published one-case failure
audit. The import binds the exact review and decision bytes, comparison and arm
report, source audit/candidate, reference/result hashes, and queue/text identity.
It records only the exact reference selection; the normal binding and successor
workspace steps still precede generation:

```bash
uv run vntts-pregenerate render-hypothesis-review-status REVIEW_DIRECTORY
uv run vntts-pregenerate render-hypothesis-review-decide \
  REVIEW_DIRECTORY accept_hypothesis
uv run vntts-pregenerate render-hypothesis-review-import \
  FRESH_AUDIT COMPARISON_DIRECTORY REVIEW_DIRECTORY QUEUE_ID
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

The human decisions were recorded on 2026-08-27:

- `314608:58` is `need_different` because the generated phrase has an
  unacceptable long pause. Its completed comparison arm must not be rerendered;
  the next attempt needs a materially different provider or reference.
- `314608:94` is `accept_hypothesis`. It authorizes importing reference 03 into
  a fresh one-case failure audit and exactly one production-shaped successor
  render.

The authorized production run completed in config-addressed workspace
`resume-395a5e5eec0327a3a793b66d-8f248017c2917708`. It produced only queue item
`reverse1999:314608:94:f6c23264391ffae3` as a pending-review PCM16 mono 48 kHz
WAV: 372,480 frames (7.76 seconds), SHA-256
`59c6f5eb48c4204adc653d3da06f245a98926e6aace304514ba055b6ff9f68a8`.
The measured longest internal silence is 0.40 seconds, silence ratio 0.0619,
and both edge silences are zero. The attempt used MOSS provider attempt 2/seed
1 for this provider (five cumulative historical attempts including three
legacy-unbound attempts). All other 481 state records retained aggregate
canonical SHA-256 `6664f2d90587...`; the source workspace state remained
`a8839038d856...`; no active attempt, lease or partial WAV remained. This WAV is
not approved and requires one normal individual human verdict.
