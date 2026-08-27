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

The real Character Story review was published without a decision at
`authoring/audio-event-reviews/current-character-story-tsk-game-v1` under the
VNTTS application-data root. Its review ID is
`938ff6f824a1fe7ebb5e98b350d77dc16f7097f19604d94f17d59a1639364ac8`;
it binds queue SHA-256
`1831f95d367e965a0a1d301e2e240dce686c4bcc23d3acae2d936675db152de7`
and the source WAV SHA-256 recorded above. `audio-event-review-status` validates
the complete bundle before replay or decision. No generation state, generated
manifest, approval or final pack was changed by publication.

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
