# Variant-aware source-reference review import

The extractor owns game-specific discovery and human source-voice decisions.
VNTTS consumes that evidence without importing extractor Python modules and
without changing an existing manifest or authoring workspace.

Publish a new self-contained plan directory:

```bash
uv run vntts-pregenerate import-reference-review \
  --report /path/to/candidates/report.json \
  --review /path/to/candidates/review.json \
  --story-index /path/to/story-index.jsonl \
  --output /new/source-reference-plan
```

The importer supports extractor review v1 and v2. V1 requires the exact report
SHA-256. V2 accepts only decisions whose candidate key, full candidate-evidence
SHA-256 and reference SHA-256 still match; invalidated decisions are preserved
as non-authoritative evidence. Report, review, story index and accepted WAVs are
read and rechecked before atomic no-overwrite publication.

Each accepted cluster is keyed by the exact `(character, portrait, source_bank)`
triple. References are copied into that cluster with their original candidate
and media identities. Queue IDs are derived from checksum-bound story records
whose `voice_character` and portrait exactly match the cluster and whose source
audio is still missing. One queue ID cannot belong to multiple clusters.
Rejected, uncertain and pending candidates never become synthesis controls.

Every plan contains the same three identity-neutral evaluation sentences. A
source acceptance is only the first gate: each cluster still requires generated
quality review on that fixed corpus. The plan deliberately does not mutate a
voice manifest, create a workspace or authorize bulk generation. The remaining
integration must bind a chosen cluster to its exact queue IDs and preserve that
binding in config-addressed synthesis controls.

Publish the evaluation inputs separately so the immutable decision plan stays
read-only:

```bash
uv run vntts-pregenerate build-reference-evaluation \
  --plan /new/source-reference-plan \
  --output /new/source-reference-evaluation
```

The evaluation directory contains a synthetic voice manifest with exactly one
accepted source WAV per variant, a generation queue with one source-matching
transcript and three fixed sentences per variant, and `comparison.json` binding
all paths and SHA-256 values. Variants are ordered by the number of affected
queue lines. Generate into a separate output directory; do not put mutable state
or rendered WAVs inside the immutable evaluation input directory. Source-match
audio can then be compared blindly against its exact original, while fixed-text
outputs compare cadence and pronunciation across variants.
