# Inline non-verbal audio events

Canonical story text may contain non-verbal directions such as `*gasp*`,
`*gurgle*`, or a vocal interjection such as `Tsk!`. These tokens are not normal
speech. Passing them unchanged to TTS produced literal spoken words in real
Character Story review, so authoring now treats them as typed composition
requirements.

## Current safe boundary

`vntts.authoring.audio_events` produces an additive version-1 plan while
preserving the exact canonical text and text SHA-256. The plan records:

- the exact spoken-text projection and its SHA-256;
- ordered event source spans, labels, typed kinds and synthesis policies;
- a canonical plan SHA-256 and an explicit `requires_composition` flag.

New extractor story indexes also carry ordered producer-owned
`story_audio_cues`. Queue planning validates every cue's positional source
fields, normalized availability, event/bank route and media lists, preserves the
complete cue records outside the VNTTS-owned plan, and binds their canonical
SHA-256 plus count into that plan. Legacy queues without the producer field keep
their exact version-1 plan shape. The binding proves which source evidence was
considered; it deliberately does not claim that an adjacent scene cue implements
the translated vocal marker.

Current recognized kinds are `human-gasp`, `human-gurgle` and `tongue-click`.
Unknown `*stage directions*` become `unsupported-stage-direction`; they are not
silently discarded or pronounced. Collection queue publication stores the plan
under reserved field `vntts.authoring.audio_event_plan` and rejects producer
collision. Legacy queues are classified again from canonical text at render
time, so rebuilding a queue is not required merely to stop literal speech.

Bulk speech generation skips every item that requires audio-event composition.
This is deliberate fail-closed behavior, not a completed audio result. It does
not spend a TTS attempt, write a WAV, infer review, or change a canonical queue
identity. A later composition publisher must retain separate speech/event WAV
hashes, ordering and mix parameters before producing one reviewable result.

A read-only scan of composed Character Story workspace
`resume-395a5e5eec0327a3a793b66d-9ec4454f35d082e4` found ten exact event lines:
seven pure effects (`whimper`, `yelp`, `pop`, three `bang` lines and `buzzzzz`),
event-only `Tsk!`, mixed `N-No! *gurgle*`, and mixed
`Wh-What! *gasp*`. All ten now return false from the ordinary speech filter.
The scan changed no queue, state, WAV or review authority.

The regenerated patch 3.7 source index now exposes the underlying evidence.
Some `pop`, `bang`, `buzzzzz` and `gasp` lines have adjacent configured story
cues, but none of those media is installed in the current game data. The two
`gasp`-line cues are stream and water-flow events, not a proven human gasp.
`Tsk!`, `gurgle`, and the relevant `whimper`/`yelp` lines have no source cue.
Therefore the current ten-line production boundary remains fail-closed.

A read-only queue plan over the regenerated `The You That's Meant To Be`
collection produced 11 event items: the ten historical workspace items plus a
separate `*shriek*` line. All 11 retain `story_audio_cues` and bind their exact
cue count and SHA-256 into the typed plan; zero-cue records bind the canonical
empty-list digest. This dry run published no queue and does not rewrite the
existing 592-item workspace.

## Model boundary

The official [MOSS-SoundEffect v2 documentation](https://github.com/OpenMOSS/MOSS-TTS/blob/main/moss_soundeffect_v2/README.md)
describes a dedicated text-to-audio model rather than a speaker-cloning TTS
model. Version 2 uses a 1.3B diffusion transformer with Flow Matching, emits
48 kHz audio, and requires its own Python 3.12 environment; the official setup
is CUDA-oriented and explicitly incompatible with the top-level MOSS-TTS
environment. Therefore an effect generated for `gasp` or `gurgle` must not be
claimed to carry the selected character voice identity.

The official [MOSS-TTS family repository](https://github.com/OpenMOSS/MOSS-TTS)
also documents the older autoregressive MOSS-SoundEffect interface. The newer
v2 model supersedes that architecture for text-to-audio quality. A third-party
MLX conversion exists, but it is not an official parity guarantee and is not a
production dependency today.

There is no CUDA machine available for the current project acceptance. Model
download/integration is therefore deferred rather than guessed. Before use, a
bounded evaluation must compare exact isolated effects, reject speech/music or
identity contamination, validate duration/rate/channel/peak/silence, and obtain
a human decision. `Tsk!` additionally permits a bounded TTS pronunciation/IPA
candidate because it is a character vocalization; it still requires blind
comparison and must never fall back to reading the letters as a word.

The bounded local pronunciation experiment is now complete. The official
MOSS-TTS v1.5 syntax accepts pure IPA inside slashes, and the installed Local
4B tokenizer round-trips both dental click `/ǀ/` and alveolar click `/ǃ/`
without an unknown token. That tokenizer fact did not translate into usable
audio: fixed-seed stable BYPASS renders with the exact Centurion conditioning
each reached the complete short-input ceiling of 3.0 seconds (144,000 samples
at 48 kHz, 300 tokens, 13 chunks) and ended typed `LIMITED`. No candidate WAV
was published and the cap was not extended.

The installed game data contains one stronger line-specific source candidate:
`reverse1999:200308:6`, Kanjira, exact text `Tsk!`, event
`play_activityvoc_hero3071_660`, bank
`activityvoc_hero3071molu1_3_part02.bnk`, media `410389900`. Read-once bank
extraction produced PCM16 mono 24 kHz audio lasting 0.751 seconds with WAV
SHA-256
`492a92aa42f2e982a05974a96e8608b24cff50db38629aa2ebe6bb24cbb46634`.
It may be reviewed only as a generic tongue-click effect; it is not Poacher I
voice provenance. A second apparent exact-text candidate through
`common_npc05.bnk` is excluded because its source/media identity is reused by
many incompatible texts and therefore does not prove a line-specific clip.

## Source-event review artifact

`vntts-pregenerate audio-event-review-publish` creates a self-contained review
directory before any human decision. It captures the exact generation queue,
requires one canonical `Tsk!` tongue-click plan, verifies the named source line
against a read-once extractor story index, copies a non-silent PCM16 mono WAV,
and binds the source line, event, bank, media, audio ID and source-index/WAV
hashes. The document fixes `speaker_identity_claim=false` and
`synthesis_voice_character=null`; accepting the effect cannot silently turn it
into Poacher I voice evidence.

The review does not touch generation state or a generated manifest. A later
`audio-event-review-decide DIRECTORY accept|reject` writes one separate atomic
no-replace terminal decision. Repeating the same decision is idempotent;
changing it or deciding against mutated queue, review or WAV authority fails.
Inspect either a pending or terminal review with
`audio-event-review-status DIRECTORY`.

Example publication shape:

```sh
uv run --no-sync vntts-pregenerate audio-event-review-publish \
  /absolute/workspace/queue.jsonl \
  reverse1999:314606:39:27d02801f93c9036 \
  /absolute/story-index-3.7.jsonl \
  /absolute/410389900.wav \
  --output /absolute/new/audio-event-review \
  --source-line-id reverse1999:200308:6 \
  --source-speaker Kanjira \
  --source-event play_activityvoc_hero3071_660 \
  --source-bank activityvoc_hero3071molu1_3_part02.bnk \
  --source-media-id 410389900 \
  --source-audio-id 610008734
```

This is review authority only. An accepted decision still needs the composition
ledger below before it may become a final line WAV.

For a pure accepted source event, publish a no-transform production composition:

```bash
uv run vntts-pregenerate audio-event-composition-publish REVIEW_DIRECTORY \
  --output COMPOSITION_DIRECTORY
uv run vntts-pregenerate audio-event-composition-status COMPOSITION_DIRECTORY
uv run vntts-pregenerate audio-event-composition-decide \
  COMPOSITION_DIRECTORY approved
```

The composition copies the accepted review, decision, queue and final WAV. Its
identity binds the event plan, game event/bank/media/source-audio evidence and
every checksum. The only supported transform is an exact byte copy at sample
offset zero, gain 1.0 and zero fades. Its ledger records
`speaker_identity_claim=false`, with no synthesis provider or synthesis voice.
The final decision is separate from the source-event suitability decision and
must be explicit before a workspace/state successor is created.

After approval, create the reviewable state successor with:

```bash
uv run vntts-pregenerate audio-event-composition-workspace \
  BASE_WORKSPACE COMPOSITION_DIRECTORY \
  --workspaces-root WORKSPACES_ROOT
```

The command holds the base generation-publication lease, copies the complete
approved composition authority into immutable workspace inputs, and binds the
exact base workspace document, generation state and replaced WAV into a second
immutable input snapshot. Their workspace/state/item/WAV hashes, plus the
composition, decision, queue and exact final WAV hashes, enter the new
configuration fingerprint. The inherited outcome-merge ledger remains
historical for only the overridden queue ID, and its original terminal item is
still verified from the copied base state. The command replaces only the
rejected rendition with the unchanged event WAV as
`generated/pending_review`; the base workspace remains byte-identical. The
ordinary individual review is still required. On approval, the same additive
ledger is projected into the approved-only manifest and is revalidated before
final-pack publication. Missing, changed or forged composition authority fails
closed.

The real Character Story review was published at
`authoring/audio-event-reviews/current-character-story-tsk-game-v1` under the
VNTTS application-data root. Its review ID is
`938ff6f824a1fe7ebb5e98b350d77dc16f7097f19604d94f17d59a1639364ac8`;
it binds queue SHA-256
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`
and the source WAV SHA-256 recorded above. `audio-event-review-status` validates
the complete bundle before replay or decision. The human accepted the exact WAV
on 2026-08-27 as a speaker-neutral tongue-click. That decision is not Poacher I
voice evidence and does not itself change generation state, a generated
manifest, approval or a final pack. Production use still requires the exact
composition ledger below.

The exact-copy production composition is now published at
`authoring/audio-event-compositions/current-character-story-tsk-game-v1`.
Composition ID
`a3750a73c90f556ca4febe366810801600acb864edcc4477cf196c0807da2245`
binds the same queue item and final WAV SHA-256
`492a92aa42f2e982a05974a96e8608b24cff50db38629aa2ebe6bb24cbb46634`.
Its ledger has sample offset zero, gain 1.0, zero fades, exact-copy bytes,
`speaker_identity_claim=false`, and no synthesis provider or voice. The final
composition was explicitly approved on 2026-08-27. That decision authorizes the
exact-copy successor workflow above; it does not by itself approve a generation
state item, project a manifest entry or identify the sound as Poacher I's
voice.

The real reviewable successor is
`resume-395a5e5eec0327a3a793b66d-a2b299862a4c4483`. Its workspace SHA-256 is
`9fa9e59f09dabadc878695d8ac418e19b439fca619c210730a115c1cb17f1146`;
its generation-state
SHA-256 is
`c53f6278d7607aaf6e2fedb4ba6d3dc33fa98aefada278e40193d42850ec03d6`.
The item is `generated/pending_review`, uses review identity `Audio Event`, has
no speech-only technical warnings, and retains exact final WAV SHA-256
`492a92aa42f2e982a05974a96e8608b24cff50db38629aa2ebe6bb24cbb46634`.
The approved-only manifest has 400 entries, including the separately approved
Narrator line 94, and deliberately excludes this pending item. Its manifest
SHA-256 is
`a5c57182d06694282e666b67eb1af59c88db0d2da7c185ff86374dbe1bc56323`.
Its copied base state SHA-256 is
`f906935a13fd124ae10d95004c56145dd4f5a95a8cc29b8aa88504ab75392ba9`;
the replaced rejected WAV remains
`a7fcc6dd2c6b9f626f3301bfe63be16fc541094681a4b1a7ee9fecd8db0c6fcd`.
Repeated creation returns the same workspace with `created=false`, and both
source and successor have no active attempt, lease or partial WAV. One ordinary
individual verdict remains before merge.

## Required composition ledger

A future accepted mixed WAV must bind all of the following:

- canonical line ID, text and text SHA-256;
- typed audio-event plan and plan SHA-256;
- spoken segment WAV, provider/model/profile/voice controls and SHA-256;
- each effect WAV, model/prompt/seed/duration controls and SHA-256;
- ordered placement, gain, fades and sample-accurate mix parameters;
- final PCM16 mono WAV metadata/SHA-256 and technical validation;
- explicit human review of the final mixed result.

Unsupported events or failed effect generation remain unresolved or use an
explicit reviewed omission. Neither path may silently publish speech-only audio
as though the event had been rendered.
