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

### Experimental silence-compression comparison

VNTTS now exposes a comparison-only `compress_single_sentence_boundary_silence`
primitive and immutable `publish_silence_comparison` bundle. This is not a
generation strategy and cannot update generation state or a generated-audio
manifest. It exists only to answer whether a derived waveform can sound as
natural as independently rendered sentence segments.

The transform fails closed unless all of these conditions hold:

- the exact story text has exactly one safe boundary between two substantial
  complete sentences;
- the PCM is finite mono audio with speech before and after exactly one notable
  internal silent span;
- the span exceeds the comparison trigger and its removable center stays below
  a stricter peak threshold than the silence detector;
- no second notable internal span exists.

Only the center is removed. The default output retains 600 ms of the original
silent boundary, split across both sides, and records exact source-span and
removed-sample indices. It does not crossfade, alter active samples, infer word
timestamps, or claim that PCM silence proves punctuation alignment. Quiet
speech, breaths, music, multiple pauses and ambiguous text therefore reject the
candidate rather than being guessed away.

`publish_silence_comparison` copies the raw failing WAV and an independently
rendered segmentation control, writes the compressed candidate, binds every
copy and report by SHA-256, validates the two reports through the standard
blind-listening loader and publishes with atomic no-replace semantics. A
standard blind session can then be created with
`create_silence_comparison_session`. Production use remains blocked until a
small real checksum-bound corpus shows equal words and speaker identity with
better cadence than segmentation. A rejected or inconclusive comparison leaves
sentence segmentation as the preferred repair.

The existing resumable generator correctly deletes `.partial.wav` after a
speech-silence validation failure and records only typed diagnostics in state.
Consequently the current immutable workspaces contain no trustworthy raw WAV
for the two unpublished long-pause failures: reconstructing one from a later
seed or another workspace would change the evidence. Before publishing the real
comparison corpus, add an explicit one-queue-ID, one-attempt evidence sink. It
must store the technically rejected WAV outside generated output, bind queue,
state, controls, attempt and failure diagnosis, and keep the artifact impossible
to review or merge as a generated outcome.

An exact comparison strategy is also available through
`--inline-pause-failed QUEUE_ID --inline-pause-ms 180`. It is restricted to one
current checksum-bound internal-silence failure, `moss-tts`, an exact queue-ID
selection and `--retries 0`. The state preserves the original queue text hash,
the derived prompt hash, marker count and pause value. This capability is not
an approval of the marker approach: the measured Dobharchú A/B and listening
gate remain outstanding.

The real carry-forward path was subsequently exercised from clean commit
`f4e9df2`. The config-addressed workspace
`resume-395a5e5eec0327a3a793b66d-ecdae3af7bcf7ede` preserved the exact current
failure for `reverse1999:314608:29:7be68e27f6d36933`: natural MOSS, seed 0,
one attempt, a 1.60-second internal silence and the single-reference Dobharchú
binding. Its one-item child command used `retries=0`; the repair attempt used
seed 1 and inserted one 180 ms marker while retaining the original story text
and text hash. The pending-review output is mono 48 kHz with:

- duration: 6.00 seconds;
- longest internal silence: 0.96 seconds;
- silence ratio: 24%;
- WAV SHA-256:
  `f7230beacb3230bb2bab6d5f9009f0d04b19ceaa3f434866843da01e007b8454`.

The unchanged 1.2-second silence gate therefore accepts the file, but this is
only a technical success. Human listening must still confirm every word,
speaker identity, cadence and the absence of an unnatural boundary. The source
workspace state remains
`493bd476e57c0723012459427fb30d58e5e98e3cc8d7c08cc14fc2645ae47b62`;
the isolated run ended with no active attempt, lease or partial WAV. No review
decision or approved manifest entry was created.

The listening sample was then expanded with four additional exact current
failures, each in its own config-addressed workspace with the same natural
profile, Dobharchú reference cluster, one 180 ms marker policy and one seed-1
attempt. Two produced pending-review WAVs:

- `reverse1999:314605:40:a15bc2a6e08da13e`: internal silence 3.52 ->
  0.64 seconds; WAV SHA-256
  `dfa9a47881374558a6ff11e6b930a961e69ebf8f6d4b5e3ed67b0f36fe3edc73`;
- `reverse1999:314605:95:ebc446c3c6e843bb`: internal silence 3.12 ->
  0.48 seconds; WAV SHA-256
  `d10a27df3f59fbece88c374d619a0433912ec1294590795ef9a8023a58a66956`.

Two other attempts failed closed and published no WAV:

- `reverse1999:314608:8:7c5e047cb7785953` retained a 2.00-second internal
  pause and 51% silent frames;
- `reverse1999:314608:27:8118276567f5deff` retained a 1.76-second internal
  pause across a three-marker, four-sentence prompt.

All four runs ended without an active attempt, lease or partial WAV. Together
with the first technically successful example, this is mixed evidence: three
of five exact attempts passed the technical gate and two did not. The marker
strategy therefore remains comparison-only until the three published WAVs are
judged for wording, speaker identity and naturalness.

The user explicitly approved the two additional technically valid WAVs. Their
source-local review states and approved-only manifests were updated for only
the exact queue IDs and hashes listed above. Commit `0652bb3` then extended the
same root-failure, queue, state, WAV and terminal-review authority checks used by
the outcome merger to the inline-marker strategy.

The two reviewed outcomes were merged non-destructively into successor
`resume-395a5e5eec0327a3a793b66d-7640ffe9b6f30ef1`. Its authoritative state
SHA-256 is
`00f7e76a2210138b2e4a17128a6db3852d56c3885b61018e1e4de82a4945bcbf`.
It contains six exact approved items: all four prior approvals plus the two new
marker repairs; its approved-only manifest contains the same six queue IDs and
both new WAV hashes. It has 214 pending-review items, 151 failures, no active
attempt, lease or partial WAV. The base and two reviewed source workspaces
remained byte-identical across the merge at tree SHA-256 values
`60a19206da51a00ce9471ad594a34611d014e55051fa14e635379d9fd3ec2017`,
`a92416ca0a0d1f3d4a802b131792036bb90bd686ae1469aca639c6f743102d8b`
and `e4675c23a510f99188fa17c6477ae8ad7bd9b562515267f40a57b30711decc38`.
The first slightly unnatural marker result remains pending and the two failed
marker attempts remain unpublished.

Inline-marker retries now share the same hard three-attempt provider budget as
bounded missed-EOS repair. Each invocation still renders exactly once with
`retries=0`; cumulative provider attempts determine deterministic seeds 0, 1
and 2. A technically valid render leaves the normal pending-review gate in
place. A third typed failure leaves no WAV, makes subsequent marker execution
fail before state mutation, and changes the deterministic failure plan to
`offline_fallback_backend`. This prevents repeated prompt-sensitive sampling
from becoming an unbounded search and preserves the unchanged speech-quality
thresholds.

The two initially fail-closed marker workspaces then exercised their final
allowed seed-2 attempts from clean commit `c4ee30c`. Both became technically
valid pending-review outputs without any extra retry:

- `reverse1999:314608:8:7c5e047cb7785953`: 1.12-second longest internal
  silence, 36% silent frames, WAV SHA-256
  `02d6c1c9d21dba7a7ebacb10e646219da9f9388607a90b9316734832b5b8eb86`;
- `reverse1999:314608:27:8118276567f5deff`: 0.96-second longest internal
  silence, 28.12% silent frames, WAV SHA-256
  `9964ea3e338b9cf113a30205624c2edcf5f186a6842a5fdd356716be80e19bf5`.

Each state records three cumulative MOSS attempts and seed 2, with no active
attempt, lease or partial WAV. Generation alone did not approve or merge either
file; the following human listening decision remained authoritative.

Listening produced different decisions despite both WAVs passing the objective
silence gate. The short line
`reverse1999:314608:8:7c5e047cb7785953` was rejected because its pauses still
sounded too large. Its repair workspace keeps the exact WAV and review evidence
as `(generated, rejected)`, while its manifest remains empty. The longer line
`reverse1999:314608:27:8118276567f5deff` was approved with exact WAV SHA-256
`9964ea3e338b9cf113a30205624c2edcf5f186a6842a5fdd356716be80e19bf5`.

Only that approved result was merged, together with the two previously approved
inline-marker workspaces, from their common immutable base. The resulting
successor is
`resume-395a5e5eec0327a3a793b66d-fce430e8b914cf3b`. Its state SHA-256 is
`4ee521380bc4941e399797855b515342ef3c82d2253074cc2610c0b207f0f8cb` and
its approved-only manifest SHA-256 is
`8c3fda5de6e1ff2b2e20c3f5a879058f667b79a72b9b22959dd64ec047ca29d5`.
The successor contains 7 approved, 214 pending-review and 150 failed items; the
manifest contains exactly the 7 approved queue IDs and excludes the rejected
short repair. The common base and all three approved source workspaces remained
byte-identical across the merge, and the successor has no active attempt,
generation lease or partial WAV.

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

The same natural failure cannot use the existing deterministic segmentation
path: its trailing clause has only two words, below the safe segment minimum.
It is now represented only by the isolated pending-review marker comparison
above; it has not been merged or approved.

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
