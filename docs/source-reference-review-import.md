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

The importer supports extractor candidate-report v1 and v2 and review v1 and
v2. Candidate-report v2 preserves whether a medium came from a story-line route
or from a complete exact-bank inventory and carries its exact Wwise event IDs.
Unrouted media is valid only with an empty source-line list, so neither the
extractor nor VNTTS invents dialogue text. Review v1 requires the exact report
SHA-256. Review v2 accepts only decisions whose candidate key, full candidate-
evidence SHA-256 and reference SHA-256 still match; invalidated decisions are
preserved as non-authoritative evidence. Report, review, story index and
accepted WAVs are read and rechecked before atomic no-overwrite publication.

Each accepted cluster is keyed by the exact `(character, portrait, source_bank)`
triple. References are copied into that cluster with their original candidate
and media identities. Queue IDs are derived from checksum-bound story records
whose `voice_character` and portrait exactly match the cluster and whose source
audio is still missing. One queue ID cannot belong to multiple clusters.
Rejected, uncertain and pending candidates never become synthesis controls.

Every plan contains the same three identity-neutral evaluation sentences. A
source acceptance is only the first gate: each cluster still requires generated
quality review on that fixed corpus. The plan deliberately does not mutate a
voice manifest, create a workspace or authorize bulk generation.

Publish the evaluation inputs separately so the immutable decision plan stays
read-only:

```bash
uv run vntts-pregenerate build-reference-evaluation \
  --plan /new/source-reference-plan \
  --output /new/source-reference-evaluation
```

The evaluation directory contains a synthetic voice manifest with exactly one
accepted source WAV per variant, three fixed sentences per variant, and
`comparison.json` binding all paths and SHA-256 values. A story-routed variant
also receives one source-matching transcript. Exact-bank unrouted media receives
no source-match item: only its fixed-corpus synthesis may be used for the
generated-quality decision. Variants are ordered by the number of affected
queue lines. Generate into a separate output directory; do not put mutable
state or rendered WAVs inside the immutable evaluation input directory.
Source-match audio can then be compared blindly against its exact original,
while fixed-text outputs compare cadence and pronunciation across variants.

After the bounded generation run, publish a self-contained cluster-specific
quality review from the exact evaluation and generation state:

```bash
uv run vntts-reference-review create \
  --plan /new/source-reference-plan \
  --evaluation /new/source-reference-evaluation \
  --state /mutable/evaluation-run/generation-state.json \
  --portrait-directory /prepared/exact-game-portraits \
  --output /new/source-reference-quality-review

uv run vntts-reference-review ui \
  --session /new/source-reference-quality-review/review.json
```

Each card represents one exact `(character, portrait, source_bank)` cluster. It
contains the original reference, every checksum-valid generated sample, the
number of story lines affected and the typed reason for every excluded result.
The UI exposes `Accept reference`, `Reject reference`, and `Need another
sample` under an explicit evidence gate. The checksum-verified original must
reach end of playback before Reject or `Need another sample` is available;
Accept additionally requires every published generated sample on the card to
reach its end. The card shows exact original/generated heard counts beside the
disabled actions. This in-dialog heard ledger is deliberately non-authoritative
and is not serialized: reopening a pending card requires hearing its evidence
again. A card without a generated sample cannot be accepted. Decisions are
serialized through an exclusive lock and copied audio is revalidated whenever
the review is loaded or played.

Decision persistence runs outside the Qt thread. The current card and
checksum-verified playback remain available while its decision buttons show a
saving phase; Close is deferred until the exclusive authority operation
finishes. A failed write leaves the card current and restores its valid actions,
so the operator can retry in the same dialog. The old immediate-decision policy
was retired because it contradicted the operator copy and allowed source quality
authority without listening evidence. A playback failure therefore fails
closed and must be repaired before the affected decision can be saved.

When `--portrait-directory` is supplied, a card copies the exact PNG whose
filename matches its story portrait identity and binds its bytes, dimensions
and SHA-256 into the immutable review. The UI renders those pixels instead of
showing an opaque portrait identifier. A card whose exact asset is absent uses
an explicit `Exact game portrait is not installed` placeholder; it never
substitutes a similarly named or web-sourced image. The absent placeholder is
compact rather than reserving portrait-sized blank space. Excluded generation
diagnostics are collapsed behind a counted technical toggle, while the selected
generated sample exposes its kind, duration and full text separately.

`vntts-listen` remains the generic blind model-comparison tool. It now supports
`Neither acceptable`, but it is not the authority for source-reference
selection: fixed-text comparisons between different character clusters answer
which voice is preferred, not whether either voice belongs to its character.

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

The first blind listening session contained nine trials: three exact original
versus generated source-match pairs and six same-text comparisons between
different character clusters. The reviewer completed one trial. That `1/9`
session is preserved as historical, non-authoritative evidence and must not be
completed or used to choose source references. The six cross-character trials
were a workflow-design error, not missing human work.

The first replacement quality review was published at
`authoring/source-reference-quality-reviews/character-story-20260818`. Its
initial `review.json` SHA-256 is
`5766f51cac1545cb0bf476cc51b1354f516c2e3ec071b2775b8462328ca002cd`.
It starts at `0/4`, contains nine copied generated WAVs and seven visible
excluded outcomes. The four cards are Dobharchú portrait `534704` (37 affected
lines, 3 generated, 1 excluded), Dobharchú portrait `534703` (11, 1, 3),
Poacher portrait `505601` (4, 2, 2), and Aderyn's Father portrait `534604`
(1, 3, 1). The historical blind session remains at `1/9` and is not binding
authority.

That session predates embedded portrait images and remains preserved at `0/4`.
The portrait-enabled successor is published at
`authoring/source-reference-quality-reviews/character-story-20260818-portraits`
with initial `review.json` SHA-256
`bc28f87548fcf2a3e7db9644f0f0fdaacb94ef5a28710699046e3bd335d32d53`.
It starts at `0/4` under a distinct directory, so the original evidence was not
overwritten. Exact installed game sprites exist for
both Dobharchú cards (`534704`, `534703`) and Aderyn's Father (`534604`). The
exact Poacher sprite `505601` was not found in the installed bundle inventory,
so its card intentionally renders the missing-asset placeholder.

After every cluster card has an explicit decision, publish the accepted set.
The binding CLI verifies that the review is complete and belongs to the exact
plan; direct variant IDs are not accepted at this boundary:

```bash
uv run vntts-pregenerate build-reference-bindings \
  --plan /new/source-reference-plan \
  --voice-manifest /path/to/base/voice-manifest.json \
  --narrator-character Centurion \
  --include-base-character Rhiannon \
  --quality-review /new/source-reference-quality-review/review.json \
  --output /new/source-reference-bindings
```

The command publishes a new, self-contained partial voice manifest; it never
edits the base manifest, decision plan or review. Every accepted variant is
bound to the exact story-derived queue IDs from its cluster. Rejected and
`needs_sample` clusters remain missing-reference preflight outcomes. Bulk
generation records the effective synthetic voice, binding-map checksum and
source queue ID in each result. Resume and final-pack publication reject a
changed map, an unselected manifest voice, or state whose effective voice no
longer agrees with that exact binding.

`--include-base-character` is explicit and repeatable. It checksum-copies only
the named base-manifest voices into the self-contained output. Use it when a
new workspace must carry reviewed full outcomes for that character; omit it to
retain the default partial-manifest behavior. Unknown, duplicate and narrator
entries fail closed.

The real portrait-enabled review was completed `4/4` with all four variants
accepted. `build-reference-bindings` published 53 exact queue-ID bindings under
`authoring/source-reference-bindings/character-story-20260818`; its manifest
SHA-256 is
`736808a315774bb84e2978df4c0cd75d5eb75cd4ce5d7942670439e880e30085`.
A new config-addressed Centurion/MOSS workspace was then created as
`resume-395a5e5eec0327a3a793b66d-ed29576f1b91239b`. Read-only inspection reported
592 queue items, 197 generated pending review, 141 failed, zero approved and a
missing-reference cohort reduced from 237 to 184. No generation, review, or
final-pack publication ran during this transition.

The expanded Character Story plan `character-story-20260818-v3` then added the
reviewed Aderyn/Rhiannon age groups and Mrs. Owen. Its immutable plan SHA-256 is
`26874659250fed238994f684f126b9a1737619af71db4768e758f1fa54945c01`.
The bounded evaluation attempted 28 fixed items once: 12 WAVs were published
and 16 typed failures retained without retry. The reviewer completed all seven
cluster cards, accepted six, and selected `needs_sample` for Mrs. Owen; the
final review SHA-256 is
`274092861821df51fbe13184cabc8b9fce413b2290b3693dbe8f3a4753377ae3`.

Bindings v4 publish those six variants for 73 exact queue IDs and explicitly
include the three checksum-bound base Rhiannon references. The manifest
SHA-256 is
`0483ed213c4001e70b436a01dccec599f753aab35dd09326f1fa46cd9ab56a3e`.
Creating workspace `resume-395a5e5eec0327a3a793b66d-682adf71694cc2d4`
carried 15 terminal Rhiannon decisions: ten approved and five rejected, with
14 legacy review-only records and one fully provenance-matched outcome. Exact
recreation returned `created=false`. Read-only inspection reports 592 queue
items, 183 generated awaiting review, 140 failed, 83 pending and 164
missing-reference lines; the earlier bindings workspace had 184 missing lines.
The source import, reviewed workspace, plan and quality-review inventories were
byte-identical before and after. No generation, new review decision or final
pack publication ran.

A later controlled pass selected exactly the 73 bound queue IDs. Every item
received one seed-0 attempt; 44 passed all WAV and speech gates. The read-only
repair plan authorized one seed-1 attempt for 15 short missed-EOS failures,
recovering eight, then one final seed-2 attempt for the remaining seven,
recovering three. The final source-bound cohort is therefore 55 generated WAVs
awaiting review and 18 typed failures. The remaining actions are 12
sentence-boundary repairs, four exhausted-primary offline fallbacks and two
human reference comparisons. The authoritative state SHA-256 is
`f3e2641f5e23307cefbe30ae48179f11c00abfebcd79351e156f3ad3cf19c2c7`.
All 323 non-carried seed records and all 15 carried Rhiannon records remained
unchanged; the approved-only manifest still has ten entries. No partial WAV,
active attempt or lease remained, and no new approval or final publication was
made.

Use the published `voice-manifest.json` when creating a new config-addressed
workspace. A partial manifest intentionally leaves unrelated queue lines in the
missing-reference preflight cohort; exact selected collection/queue retries can
proceed, but an unfiltered run remains blocked until its own references or an
explicit fallback policy are available.
