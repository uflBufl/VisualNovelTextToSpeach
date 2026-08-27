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
