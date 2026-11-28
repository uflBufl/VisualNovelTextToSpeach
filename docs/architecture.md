# Architecture

VNTTS keeps reading, waveform generation, playback and content preparation at
separate boundaries. The [documentation index](README.md) lists the guides and
contracts for each part.

## Reading and playback

The desktop controller composes capture, recognition, voice preparation and
navigation. Capture supplies a selected window or screen region, and OCR turns
it into observations used by the reading state machine. The normal reader can
speak OCR with the configured voice. A compatible story index can enrich that
flow with identity, original audio and prepared recordings. An explicit
sequence plan can provide stricter navigation authority.

- [Controller architecture](controller-architecture.md)
- [Text-reading architecture](text-reading-architecture.md)
- [Sequence-first live reading](sequence-first-live-reading.md)

The audio router binds each accepted line to its playback path. Synthesis
backends produce typed PCM results independent of the audio device. The native
audio worker owns playback for application-generated speech and prepared clips.
Auto advance depends on route completion and acknowledgement of the game
transition.

- [Live audio routing](live-audio-routing.md)
- [Synthesis rendering](synthesis-rendering.md)
- [Live replay acceptance](live-replay-acceptance.md)

## Content preparation

The player can prepare installed stories through **Stories**. Specialist
authoring tools also work with explicit inputs and maintain their own
generation and review state. Both workflows can produce artifacts for the
runtime. Game-specific extraction lives in the separate Reverse: 1999
extractor. The runtime imports versioned packs through one checksum-bound
boundary.

- [Self-service pregeneration](self-service-pregeneration.md)
- [Authoring module boundaries](authoring-module-boundaries.md)
- [Game-pack publication](authoring-game-pack-publication.md)
- [Game-pack import](game-packs.md)
