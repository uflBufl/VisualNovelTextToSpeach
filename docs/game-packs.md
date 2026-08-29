# Game-pack import boundary

VNTTS consumes the released `vntts.game-pack` version 1 and 2 contracts from
`vntts-artifacts` v0.7.1. A pack manifest binds a required versioned story
index and voice manifest, every referenced voice WAV, optional generated audio
and live-sequence-plan components, the producing tool versions, and a SHA-256
digest for each file.

`vntts.game_pack.import_game_pack(path)` is the public preflight entry point.
It delegates schema, safe-path, component and checksum validation to the shared
contract and returns a `GamePackImport` containing absolute paths for the four
VNTTS inputs. `GamePackImport.apply_to(settings)` and
`apply_game_pack(settings, path)` return a new immutable `AppSettings`; neither
copies, deletes nor rewrites pack or application data. A pack without a
generated-audio component explicitly clears a stale generated-audio setting.

The **Game pack** setting and `VNTTS_GAME_PACK` environment variable run the
same preflight when settings are loaded. The checksum-bound pack components
take precedence over separately saved story-index, voice-manifest and
generated-audio and live-sequence-plan paths. Game profiles persist the pack
manifest path and repeat preflight before a profile is activated. Invalid or
modified packs are rejected instead of silently falling back to stale component
paths. Importing a version-1 pack, or a version-2 pack without a live sequence,
clears any stale standalone live-sequence setting.

To validate a delivery without starting the desktop application:

```sh
uv run vntts-preflight-game-pack /path/to/game-pack.json
```

The command prints the validated game identity and resolved input paths as
JSON. It is read-only.

## Runtime routing after preflight

Pack validation establishes artifact identity; it does not itself select an
audio source. Live routing uses the validated story/generated components with
this fixed precedence:

1. a manual character or Narrator voice override forces live TTS;
2. `live-tts-only` selects live TTS;
3. `prefer-game-audio` selects an exact, declared-available source line while
   live mode is active;
4. `prefer-game-audio` or `prefer-generated` may select an exact or uniquely
   normalized generated entry at speed `1.0`; and
5. every remaining case uses live TTS with an explicit trace reason.

Source audio is game-owned. A positive `source_audio_duration_seconds` under
the declared duration completion policy creates a conservative delay from route
acceptance; it is not observation of game-device playback. A source pass
through with no duration remains `passthrough-unobserved`, seals the exact line
against duplicate suffixes, and cannot authorize auto advance. Missing timing
never authorizes live-TTS replacement of source audio because the game is
already speaking the line; the operator advances that line manually until the
producer supplies a trustworthy duration.

Generated audio is VNTTS-owned. The consumer reads a contained WAV once, hashes
that byte snapshot, validates its manifest/PCM metadata, and decodes those same
bytes. Unique early-prefix expansion carries a reservation for that verified
PCM and canonical text. If the reservation becomes invalid, expanded text is
discarded instead of being spoken through live TTS. Short, ambiguous, missing,
tampered, or nondefault-speed candidates use the ordinary exact/fallback path.

Live synthesis uses call-bound typed preparation/playback outcomes and the
backend's `USE` cache policy. Only complete renders enter memory or persistent
speech caches; source and generated routes bypass them. A completed generated
or observed source route seals the exact OCR generation and may permit auto
advance. Live TTS remains tracker-driven. Interrupted or failed playback does
not seal and blocks automatic dispatch. Every route, voice, first-PCM,
completion and outcome event is keyed by privacy-safe chunk ID, preserving
multiple chunks inside one OCR generation.

Eligibility is not confirmation. VNTTS sends at most one automatic key for an
eligible dialogue generation and records a pending transition. Only OCR seeing
a new dialogue generation or an empty-dialogue transition confirms success.
Paused live reading or temporary focus loss postpones dispatch or confirmation.
The initial grace period is a nonterminal waiting state. If the full bounded
confirmation window expires, VNTTS keeps watching for later dialogue but never
retries the key for that generation, because a second key could skip a line;
the UI requests manual advance instead.

The single-backend benchmark is render-only: it verifies fresh, memory and
persistent cache stages when the backend exposes them, without opening an output
device. Underrun, driver jitter, stop latency and source/game timing remain
hardware acceptance work.
Concrete backend `prepare()`/`play()` and mutable `last_*` values exist only as
a deprecated external compatibility facade; runtime routing and replay consume
typed outcomes. See [typed live audio routing](live-audio-routing.md) and
[device-independent rendering](synthesis-rendering.md).

Producer/consumer compatibility is covered by a synthetic contract test. It
uses the public shared writers to publish a story index, voice manifest,
generated-audio manifest, referenced PCM WAVs and a complete pack, then imports
that pack through the public VNTTS boundary and loads all three inputs through
the runtime chapter, voice and generated-audio consumers. The test also checks
that tampering with a referenced WAV fails preflight and that unrelated
application data remains untouched.
