# Dobharchú repair comparison

The current Character Story workspace contains 50 exact Dobharchú queue
items. The checksum-bound census on 2026-08-21 found:

- 15 approved WAVs, which remain authoritative and are excluded from repair;
- 22 rejected WAVs;
- 11 typed `audio limit / missed EOS` failures;
- two absent queue items with no exact queue-to-reference binding.

The rejected, failed and absent records are represented by immutable comparison
plan `4ce01ad4f6c047cf430235fdccfc8d48e9135c2295af809132451e4515585e7c`.
Its canonical file SHA-256 is
`9f3c902281791a6acefcc2192818207a2241e7300a751ede260a8b9d067e3609`.
The plan is stored under the VNTTS application-data review-bundle directory as
`current-character-story-dobharchu-repair-comparison-v1.json`.

The plan binds the exact workspace, queue, generation state, voice manifest,
model tree, ordered reference WAVs, queue item identities and existing WAV
hashes. Publishing it did not mutate generation or review state. It records 35
repair targets: 33 have exact reference-variant bindings and two remain blocked
as `exact_reference_variant_unbound`. Those two IDs are:

- `reverse1999:314608:95:965bd814a6e36dbf`
- `reverse1999:314608:96:5e1fe5bdc801e728`

## Bounded comparison

The two candidates use the same MOSS model and exact portrait-specific
reference WAVs. They differ only in supported generation profile: `stable`
versus `natural`. Token-level duration control remains disabled. Pocket is not
used as a Dobharchú identity candidate because it does not clone either exact
portrait reference.

The deterministic comparison set contains one available short, medium and long
unresolved item for each exact portrait variant, five items in total because
one variant has no unresolved short item:

- `reverse1999:314602:94:bc0c0eaa3b459b09`
- `reverse1999:314608:40:d2a840395a023447`
- `reverse1999:314605:83:36000991eea08abf`
- `reverse1999:314608:29:7be68e27f6d36933`
- `reverse1999:314605:87:30d3291b0cd792b0`

Generate these exact IDs in separate config-addressed successor workspaces,
then compare matching lines. Review every generated result carrying a technical
attention flag and expand a portrait variant only when its deterministic sample
finds another substantive voice, pronunciation, pacing, pause or contamination
defect. The review projector flags speech below 110 WPM and internal silence of
at least 0.5 seconds from checksum-bound WAV bytes.

The plan can be reproduced without running a model or changing state:

```bash
uv run vntts-pregenerate voice-repair-comparison-plan \
  WORKSPACE 'Dobharchú' \
  --generation-profile stable \
  --generation-profile natural \
  --output COMPARISON.json
```

Publication is no-replace. Rebuilding against changed state, queue, references,
manifest or model produces a different plan or fails closed.

## Candidate preparation

Each candidate is prepared from the immutable legacy import rather than by
copying the mutable primary review state. The command publishes a self-contained
voice-manifest bundle, copies every referenced WAV by exact digest, records the
plan and candidate identities in the manifest, and then creates a
config-addressed resume workspace:

```bash
uv run vntts-pregenerate voice-repair-candidate-workspace \
  COMPARISON.json CANDIDATE_ID IMMUTABLE_IMPORT \
  --inputs-root CANDIDATE_INPUTS
```

The bundle has a canonical, duplicate-free inventory and refuses traversal,
symlinks, changed bytes, non-canonical ordering, an existing conflicting
destination, or a stale source plan. Repeating the exact preparation is
idempotent and reports that neither input nor workspace was newly created.

Before generation, obtain and inspect the child command independently:

```bash
uv run vntts-pregenerate voice-repair-candidate-command \
  COMPARISON.json CANDIDATE_ID CANDIDATE_WORKSPACE
```

The command rebinds the plan, workspace run configuration, candidate manifest,
model control and exact sample scope. It contains the five listed queue IDs and
does not select all pending items. Candidate generation must run only from a
clean verified source commit, one profile at a time, with the primary workspace
and every unrelated candidate seed record checked before and after the child.
