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
The primary review-state SHA-256 stayed
`de93ffd0286be2b41f47689f97025d8290c950c5caf39939b262b26960c4c2d7`,
the unrelated candidate digest stayed unchanged, and both runs ended with no
active attempt, lease or partial WAV.

## Human decision

On 2026-08-21 the three exact natural WAVs in the table were heard and approved.
The two 0.64-second pauses were judged natural for these exact performances;
they are not repair defects. This decision applies only to the three listed WAV
hashes and does not weaken the general speech-silence gate.

The candidate review state now records all three items as `approved`, and its
derived manifest contains exactly those three entries. The resulting state
SHA-256 is
`a3d51aa5a84ec9d07582d2c4ddf6e8bc0c5e7c0097921d2d287272d84ac3959d`.
The primary review-state SHA-256 remained unchanged, all unrelated candidate
items remained byte-identical, and the review ended with no active attempt or
lease. Natural is the accepted Dobharchú profile for both reviewed portrait
variants. The two unpublished natural failures still require a separate,
bounded sentence-boundary repair rather than a global silence-policy change.

Portraits `534703` and `534704` were also confirmed to show the same person with
different facial expressions. Treat them as expression aliases for review and
coverage while preserving both source portrait IDs, exact reference WAVs and
synthesis provenance. The reusable contract is documented in
[`portrait-expression-aliases.md`](portrait-expression-aliases.md). This
decision does not include unbound portrait `534705`.

## Composite-reference pause attribution

An isolated probe concatenated the accepted 2.38-second `534704` reference and
1.73-second `534703` reference after trimming only outer silence, with a 150 ms
neutral gap. The resulting 4.163958-second PCM16 mono prompt has SHA-256
`158c32792ebed2f75b325ede3785758e63e094d6db1b6554fd7df13161776d39`.
It was encoded once and rendered with the production `stable` profile, cache
policy `bypass` and no manifest, state or review mutation.

Checksum-bound controls rendered the same text, selected seed and profile using
only the accepted 2.38-second reference. Silence was measured in 10 ms windows
below -40 dBFS:

| Text shape | Composite pause | Single-reference pause |
| --- | ---: | ---: |
| `What happened? You're hurt.` | 3.07 s | 3.52 s |
| one sentence, nine words | 0.15 s | 0.25 s maximum |
| two complete longer sentences | 2.78 s | 3.01 s |

The prompt itself has only a 0.24-second silent interval at its join. Both
conditioning variants therefore reproduce the multi-second pause while the
single-sentence control does not. The composite join is not the cause and does
not solve the defect. MOSS emits a long run of silent audio tokens at these
sentence boundaries. Token-level duration control is disabled, and the
text-derived safety limit only bounds missed EOS; neither forces this pause.

Use the existing bounded sentence-boundary repair when every resulting segment
meets the safe minimum: render segments independently and join them with the
declared 180 ms pause before applying the unchanged speech-quality gate. The
long probe is eligible. The short probe is intentionally not eligible because
both clauses contain only two words and `safe_sentence_segments` preserves it
as one unit; use a bounded seed/reference comparison and then the typed offline
fallback backend if no quality-valid MOSS render exists. Do not force token
duration, rewrite punctuation globally or weaken the silence gate. The
composite may still be judged for speaker consistency, but it is not a pacing
repair.

### External implementation evidence

The upstream MOSS-TTS v1.5 model card documents explicit inline pause markers
such as `[pause 0.2s]` and token-level duration control. Its reference app keeps
duration control disabled by default and exposes decoding parameters, but does
not provide an automatic unwanted-silence repair. This makes one bounded A/B
useful: compare independent safe sentence rendering with a derived synthesis
prompt that replaces only the matching sentence boundary with
`[pause 0.18s]`. The original story text and text hash must remain unchanged,
and the marker candidate must pass exact content, identity, prosody and silence
review before it can be used.

The official MOSS-TTS-Nano runtime and the community MOSSTTSKit long-text path
both split text at sentence-ending punctuation first, then at clause boundaries
and finally by token budget. They concatenate independently rendered chunks
with a short declared pause. Although those implementations target Nano rather
than this repository's Local Transformer 4B backend, they independently support
sentence/clause chunking as the first repair to test rather than a global
waveform or punctuation rewrite.

A Sokuji issue for MOSS-TTS-Nano reports the same observable failure class:
multi-second silence is present in generated PCM, is stochastic and is affected
by the speaker prompt. Its temperature and repetition-penalty sweeps were not a
reliable cure. That report calls the behavior a silence-token attractor and
suggests prompt selection, bounded silence compression and runaway-silence
handling. It is not direct proof for Local Transformer 4B, so local
checksum-bound controls remain authoritative. In particular, this project must
not stop and publish at the first long silence: valid speech can follow it. Any
streaming cutoff may only fail the current already-segmented unit so that it can
be retried or routed to a typed fallback without publishing truncated speech.

Sources:

- [MOSS-TTS v1.5 model card](https://github.com/OpenMOSS/MOSS-TTS/blob/main/docs/moss_tts_model_card.md)
- [MOSS-TTS v1.5 reference app](https://github.com/OpenMOSS/MOSS-TTS/blob/main/clis/moss_tts_app.py)
- [official MOSS-TTS-Nano sentence/clause splitting](https://github.com/OpenMOSS/MOSS-TTS-Nano/blob/main/onnx_tts_runtime.py)
- [MOSSTTSKit long-text support](https://github.com/kyinwind/MOSSTTSKit#long-text-support)
- [Sokuji MOSS-TTS-Nano long-silence investigation](https://github.com/kizuna-ai-lab/sokuji/issues/277)

The authoring implementation now records a versioned pause diagnosis for every
current speech-silence failure. It binds every notable span of at least 0.5
seconds as leading, internal, trailing or all-silent, together with the text
shape, safe-segmentation eligibility, provider, model, profile, seed and exact
synthesis-control digest. Generic failures are labelled only as a
`sentence_boundary_pause_candidate`, because PCM silence plus punctuation does
not prove phoneme alignment. The existing sentence-boundary strategy remains
the primary deterministic repair for substantial complete clauses.

An exact comparison strategy is also available through
`--inline-pause-failed QUEUE_ID --inline-pause-ms 180`. It is restricted to one
current checksum-bound internal-silence failure, `moss-tts`, an exact queue-ID
selection and `--retries 0`. The state preserves the original queue text hash,
the derived prompt hash, marker count and pause value. This capability is not
an approval of the marker approach: the measured Dobharchú A/B and listening
gate remain outstanding.

## Internal-silence repair result

Commit `149f895` added a bounded sentence repair for a typed
`speech_silence` failure only when the text has at least two safe complete
sentences, leading and trailing silence remain within their existing limits,
and the internal-pause limit is exceeded. The normal post-render silence gate
still applies to the joined result.

The repair workspace
`resume-395a5e5eec0327a3a793b66d-1f38c152d8a1b13e` carried only the exact failed
outcome for `reverse1999:314605:87:30d3291b0cd792b0` from the natural candidate.
Its child command selected only that queue ID with `retries=0`. It rendered the
three exact complete sentence segments with seeds 1, 2 and 3 and inserted the
configured 180 ms boundary pause. The result is a pending-review mono 48 kHz
WAV:

- duration: 11.96 seconds;
- longest internal silence: 0.72 seconds;
- silence ratio: 19.33%;
- WAV SHA-256:
  `aa4d5e0d6313b202c41d7bf7201c67c1166e3c347bb51b61a2904fc68244acf3`.

All 337 unrelated authoritative state items equal the immutable import seed,
the source natural candidate state remains
`a3d51aa5a84ec9d07582d2c4ddf6e8bc0c5e7c0097921d2d287272d84ac3959d`,
and the run ended with no active attempt, lease or partial WAV.

The exact repaired WAV was then heard and explicitly approved. Its review state
is `(approved, approved)`, the repair manifest contains exactly that one queue
ID and WAV digest, and the resulting repair-state SHA-256 is
`3b47d193207bc459ed8c95d02a9a3d8e3ed5424b00380101041f4d149f884e03`.
The approval was merged non-destructively with the three accepted natural
samples into immutable successor
`resume-395a5e5eec0327a3a793b66d-b3a3c14c9725777a`. Its authoritative state and
approved-only manifest contain exactly the four reviewed Dobharchú queue IDs;
the merge ledger binds the source repair workspace, source state, queue item
and WAV digest. The natural candidate and repair source states remained
byte-identical, and the successor has no active attempt or lease. This approval
is checksum-bound to the listed WAV and does not approve other synthesis
outcomes automatically.

The other natural failure,
`reverse1999:314608:29:7be68e27f6d36933`, remains intentionally blocked. Its
trailing sentence fragment has only two words, below the existing safe segment
minimum, so it was not retried.

## Natural expansion and unified review gate

The accepted natural profile was expanded from successor
`resume-395a5e5eec0327a3a793b66d-b3a3c14c9725777a` to the exact remaining 28
bound targets. The child command used all and only those queue IDs with
`retries=0`, base seed 0 and no existing-item regeneration. It produced 17
pending-review WAVs and 11 typed failures. The four existing approvals, their
approved-only manifest, all 342 unrelated base items and the merged repaired
WAV remained unchanged. The resulting source-state SHA-256 is
`493bd476e57c0723012459427fb30d58e5e98e3cc8d7c08cc14fc2645ae47b62`;
the run ended with no active attempt, lease or partial WAV.

Ten failures were independently classified as safe sentence-boundary repairs.
Config-addressed carry-forward workspace
`resume-395a5e5eec0327a3a793b66d-3e8158ddf2fdb81a` selected exactly those ten
queue IDs and ran each once. Seven repairs published pending-review WAVs; three
remained typed failures. The source-state SHA stayed byte-identical, the repair
manifest remained empty, and its final state SHA-256 is
`d680335a26f9000522904a3bfe05ecb0a3ee8364df1aeadb0834c3093de3d427`.
No active attempt, lease or partial WAV remained.

The 17 direct WAVs and seven successful repairs are bound into one immutable
multi-workspace review bundle:

`current-character-story-dobharchu-natural-expansion-v1.json`

Its bundle ID is
`d0f42e5eab5476e38a849ac0dcc27acab6ae65efb24ebd35bdc3eedd12779371`.
It contains two source workspaces, four exact cohorts, 24 pending items, 24
mandatory samples and zero blocked items. Open it with:

```sh
uv run --no-sync vntts-review-bundle \
  "$HOME/Library/Application Support/VisualNovelTextToSpeech/authoring/review-bundles/current-character-story-dobharchu-natural-expansion-v1.json"
```

Human listening remains required before any cohort decision. No direct or
repaired WAV from this expansion was approved automatically. Three attempted
sentence repairs and two reference-comparison cases remain outside the bundle;
their exact IDs and retry restrictions stay in `todo.md`.
