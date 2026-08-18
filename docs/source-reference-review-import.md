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

After the bounded generation run, publish strict listening reports from the
exact evaluation and generation state:

```bash
uv run vntts-pregenerate build-reference-listening-reports \
  --evaluation /new/source-reference-evaluation \
  --state /mutable/evaluation-run/generation-state.json \
  --output /new/source-reference-listening-reports

uv run vntts-listen start-reports \
  --reports /new/source-reference-listening-reports/*.json \
  --output /new/source-reference-listening-session \
  --seed 0
```

The report publisher includes only checksum-valid generated outcomes. A
successful source-match render is paired with its exact original reference;
successful fixed sentences are paired across all variants that produced the
same text. Failed or limited renders remain visible in generation state and do
not become listening audio. Model identities are stored only in the private
0600 blind key; the public session exposes randomized A/B aliases.

## Verified Character Story evaluation (2026-08-18)

The first real plan contains four accepted variants: Dobharchú portraits
`534704` and `534703`, Poacher `505601`, and Aderyn's Father `534604`. Their
exact affected-line counts are 37, 11, 4, and 1. The immutable plan SHA-256 is
`23f84370ef520d819843f3485760b4cfc66cd06d88475beaa8f9e03637450142`.

The bounded MOSS run attempted 16 items once with seed 0. Nine checksum-valid
WAVs were published; five renders ended at the typed audio limit and two failed
the speech-silence gate, so no WAV was published for those seven. No retries,
review decisions, bulk workspace changes, or manifest approvals were made. The
state SHA-256 is
`9df9dee7a47515b5332633aac90fb46b4cc8ed207d5086b68694657835a3ebad`.

The resulting listening session has nine trials: three exact original versus
generated source-match pairs and six same-text comparisons between successful
variants. It starts at 0/9 and remains a manual gate. Its public session SHA-256
is `6218f26672df477dbc55fb5ff336cf9236b902351336b66d1437a342e28b126e`;
the private blind key is mode 0600 and checksum-bound by the public session.
