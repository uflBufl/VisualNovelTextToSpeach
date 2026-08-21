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

Both source records use portrait `534705.png`. A read-only audit of the current
decrypted config tables found no literal `534705` identity or audio route. The
story records themselves have blank voice IDs and no bank, event or media ID;
the installed same-speaker evidence covers only the separately reviewed
`534703` and `534704` portrait groups. There is therefore no exact local source
fact that can assign either existing reference to `534705`. These two lines
remain intentionally unbound until a new source asset or an explicit human
same-voice decision is published.

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

## Verified candidate run

The two candidates were prepared and run from clean commit `1ab96b8` on
2026-08-21. Exact preparation was idempotent for both the input bundle and
workspace. The stable workspace is
`resume-395a5e5eec0327a3a793b66d-fbdf3d6391ee18ad`; the natural workspace is
`resume-395a5e5eec0327a3a793b66d-a8643584acb0cd86`. Both began with 338 state
items, none of the five sample IDs, no active attempt and no lease. Their
unrelated canonical item digest was
`cc9fc1b5afda65a1e334210b266bffbad7f38fdf25864ef398594ed44578bcd0`.

The stable profile published no WAVs. Three lines reached their bounded audio
limit and two completed rendering but failed the speech-silence gate with
3.20-3.28 seconds of internal silence. All five remained failed.

The natural profile published three pending-review mono 48 kHz WAVs:

| Queue ID | Variant | Duration | Internal silence | WAV SHA-256 |
| --- | --- | ---: | ---: | --- |
| `reverse1999:314602:94:bc0c0eaa3b459b09` | `cluster-2f4d52a49d13c24bbd0e74ad-anchor-1` | 4.72 s | 0.64 s | `98036fbbfc14b7ca9874780177471bcbc87286ac525fc0f0c1719c8658dd92dc` |
| `reverse1999:314608:40:d2a840395a023447` | `cluster-2f4d52a49d13c24bbd0e74ad-anchor-1` | 8.40 s | 0.64 s | `494d0bd50ca4fc4222579f5f6143e392327c08419142b94c3febf498b2b9b981` |
| `reverse1999:314605:83:36000991eea08abf` | `cluster-e8dcae5254441ab7633ba7d9-anchor-1` | 1.92 s | 0.00 s | `94d38f9366e221aa08d8c8cbf3248cea6dda659d980b372dec3834d102ea91b8` |

The other two natural lines completed rendering but failed the speech-silence
gate with 1.60 and 1.36 seconds of internal silence, so no WAV was published.
No candidate was approved or rejected. The primary review-state SHA-256 stayed
`de93ffd0286be2b41f47689f97025d8290c950c5caf39939b262b26960c4c2d7`,
the unrelated candidate digest stayed unchanged, and both runs ended with no
active attempt, lease or partial WAV. Natural is therefore the only candidate
with listenable evidence, but it is not authorized for expansion until the
three exact WAVs receive a human voice/pacing decision.
