# Typed live audio routing

Live dialogue routing is a two-phase contract. `prepare_route()` returns one
immutable decision: `SourceAudioRoute`, `GeneratedAudioRoute`, or
`LiveTTSRoute`. Each decision binds its exact payload, line identity,
preflight state, fallback reason, source trace, and preparation timing. A later
`play_route()` returns a route-local `PlaybackOutcome`; controller correctness
does not depend on mutable backend `last_*` fields. The outcome keeps the
decision's effective source, synthesis and first-audio timing, cache source,
playback timing, underrun state, and completion status together, so concurrent
preparation cannot mix metrics from different chunks.

The selector keeps the established precedence:

1. a manual voice override or `live-tts-only` policy selects live TTS;
2. active live mode with `prefer-game-audio`, a resolved declared-available
   source, and any required completion duration selects game pass-through;
3. `prefer-game-audio` or `prefer-generated` may select a verified generated
   artifact at speed `1.0`; and
4. remaining exact, normalized, ambiguous, missing, unsafe, or policy-skipped
   cases use live TTS with an explicit fallback reason.

Generated WAV verification reads bytes once, hashes those exact bytes, and
decodes the same snapshot. Early-prefix expansion reserves that verified PCM;
if the artifact is missing or invalid before reservation, expansion is not
eligible, and a reserved canonical line never falls through to live synthesis
after a later file mutation.

Playback outcomes are `completed`, `interrupted`, `failed`, or
`passthrough-unobserved`. Generated playback alone owns the local output device.
Stopping source pass-through cancels only its completion timer. A guard or stop
before output produces no first-PCM observation; interruption or failure blocks
auto advance for that OCR generation and never seals it. A source route without
observable completion is sealed as the exact line but explicitly blocks auto
advance. Player exceptions are recorded as a chunk-bound failed outcome before
the normalized playback error is surfaced.

Route, voice-resolution, generation-start, first-PCM, completion, outcome, and
suppression timeline records are keyed by chunk ID. Duplicate reports for the
same stage/chunk merge, while multiple chunks in one OCR generation remain
distinct. Live synthesis continues to use the backend's existing cache policy;
source and generated routes bypass live synthesis caches.

`GeneratedAudioFallbackBackend` is an internal route selector/player, not an
exported backend API. All repository callers use `prepare_route()` and
`play_route()`; it therefore has no payload-only `prepare()`/`play()` facade and
does not mirror outcomes through mutable `last_*` fields. No deprecated alias is
kept because this wrapper was never an application entry point or documented
extension interface. The individual live TTS backends now provide typed
`prepare_playback()`/`play_prepared()` calls. Their legacy payload methods and
mutable metrics are retained only as the deprecated external compatibility
facade described in `synthesis-rendering.md`; the controller and replay paths
do not consume them.

Real driver underrun, device-stop latency, game-audio alignment, focus/key
confirmation, and long-soak behavior remain hardware acceptance gates rather
than software unit-test claims.
