# Typed live audio routing

**Status: current runtime contract.** The implementation is in
`vntts/generated_audio.py`; route consumers use typed playback outcomes. For
setup and player controls, use [Playing](playing.md). Sequence identity and
advance ownership are defined in [Sequence-first reading](sequence-first-live-reading.md).

Live dialogue routing is a two-phase contract. `prepare_route()` returns one
immutable decision: `SourceAudioRoute`, `GeneratedAudioRoute`,
`PendingGeneratedAudioRoute`, `AudioEventOmissionRoute`, `LiveFallbackRoute`,
or `LiveTTSRoute`. Each decision binds its exact payload,
line identity, preflight state, fallback reason, source trace, and preparation timing. A later
`play_route()` returns a route-local `PlaybackOutcome`; controller correctness
does not depend on mutable backend `last_*` fields. The outcome keeps the
decision's effective source, synthesis and first-audio timing, cache source,
playback timing, underrun state, and completion status together, so concurrent
preparation cannot mix metrics from different chunks.

The selector keeps these boundaries:

1. In active live mode, exact original speech verified as a full reading with
   authoritative timing remains game-owned under every audio-source policy.
   It also takes precedence over a voice override, preventing a second reading
   over the game's complete speech. Main-story source authority may require
   the configured source resolver to verify timing before routing; failure
   blocks that route rather than inventing a duration.
2. An exact authorized audio-event omission is a distinct non-playing route.
3. Otherwise manual voice overrides select live synthesis. The generated
   policies can use checksum-verified prepared audio only at speed `1.0`.
4. A partial game-owned cue under `prefer-game-audio` waits for its duration
   plus 350 ms post-roll, then uses the full generated/live reading. The wait
   can be anchored to first text observation so time already elapsed is not
   counted again.
5. When a bound library has durable generation in progress, an uncovered line
   can return `PendingGeneratedAudioRoute`. Playback waits for a verified
   published artifact or an explicit terminal decision; a temporarily missing
   WAV is not permission to synthesize a competing version.
6. A backend-compatible explicit fallback decision routes through
   `LiveFallbackRoute`; remaining eligible cases use `LiveTTSRoute` with a
   recorded reason. `live-tts-only` skips generated selection, but does not
   override confirmed full original speech during live reading.

One-shot reading is a deliberate application playback and does not silently
pass through game-owned audio. Source availability alone, or measured duration
with unknown semantic completeness, cannot authorize the full-original route.

Generated WAV verification reads bytes once, hashes those exact bytes, and
decodes the same snapshot. A sufficiently long unique indexed prefix expands to
the full canonical line before routing, even when that line has no generated
artifact. This keeps a missing-WAV fallback to one complete live synthesis
instead of successive typewriter fragments. When OCR loses or corrupts the
nameplate, a unique text prefix may also restore its canonical speaker, but only
inside the already established chapter; short, ambiguous, cross-chapter, and
non-prefix observations remain unmatched. A matching generated artifact is
reserved as verified PCM, and that reserved canonical line never falls through
to live synthesis after a later file mutation.

An OCR body consisting exactly of `...` or `…` is an intentional silent
dialogue, not background noise and not pronounceable text. OCR cleanup preserves
it and the incremental tracker commits it without scheduling speech. In
sequence-owned `audio-auto`, the stronger authority is the unique immediate
line-less silent successor after one application-owned key dispatch: a focused,
changed and settled dialogue frame may confirm that event without depending on
OCR's ability to read three dots. The same focus-checked auto-advance gate may
then continue. Other punctuation-only glyphs remain filtered.

Auto advance requires an exact captured game window that is still the foreground
window at dispatch time. A calibrated screen region has no authoritative window
identity and therefore never sends a global advance key; Settings and onboarding
disable that combination instead of treating a missing target as focused.

Playback outcomes are `completed`, `interrupted`, `failed`, or
`passthrough-unobserved`. Generated routes own their explicit local output
stream; source pass-through owns no output device. Stopping pass-through cancels
only its completion timer. A guard or stop
before output produces no first-PCM observation; interruption or failure blocks
auto advance for that OCR generation and never seals it. A source route without
observable completion is sealed as the exact line but explicitly blocks auto
advance. It never falls through to live TTS merely to obtain a completion
timer: the game has already spoken that line, so such a fallback would create
audible duplicate dialogue. Player exceptions are recorded as a chunk-bound
failed outcome before the normalized playback error is surfaced.

Generated playback must not use `sounddevice.play()`: that convenience API
owns process-global stream state, while live backends use explicit
`OutputStream` instances. Mixing those ownership models previously produced
incorrect playback duration across routes with different sample rates.

The generated route therefore resolves the output device rate, resamples its
immutable PCM once, and writes mono float32 frames through its own explicit
blocking `OutputStream`. Stop aborts that exact stream rather than a
process-global convenience stream. Its typed outcome records source rate,
playback rate, output sample count and expected PCM duration next to observed
playback time. These fields distinguish a device/stream-format failure from a
bad checksum-valid generation; the latter must not be rejected or regenerated
solely because runtime playback negotiated the wrong format.

`source_audio_status=available` proves that the configured Wwise event and
media exist; it does not prove that the media reads the displayed sentence.
The extractor publishes raw sample-count duration and media SHA-256
separately from semantic completeness. A duration-impossible full reading can
be proven `partial`; plausible duration alone stays `unknown`. Only verified
`full` completeness with authoritative timing permits the full-original route.

Route, voice-resolution, generation-start, first-PCM, completion, outcome, and
suppression timeline records are keyed by chunk ID. Duplicate reports for the
same stage/chunk merge, while multiple chunks in one OCR generation remain
distinct. In sequence-audio modes, one cursor event may complete canonical
playback once; later prepared or queued audio for that same event is suppressed
until an explicit reanchor or a transition gives the cursor a new event. Live
synthesis continues to use the backend's existing cache policy; source and
generated routes bypass live synthesis caches.

The persistent live-synthesis cache implements bounded LRU eviction with file
mtimes. Each successful read or write advances its entry past the newest known
cache timestamp by at least one millisecond. This preserves ordering on Windows
and other filesystems that assign identical mtimes to rapid writes; eviction
never falls back to arbitrary or lexical cache-key order.

Speaker-change announcements are a configurable layer above this selector.
The current default is `narrator-fallback-roles`; `off` disables the layer.
`all-speakers` announces changes in the visible speaker; unattributed `???`
uses the spoken label `Unknown`. The announcement is a separate Narrator
`LiveTTSRoute`, preceding the selected canonical dialogue.

The narrower `narrator-fallback-roles` mode derives a generated route's role
from its validated lossless producer record. A named
`missing_voice_to_narrator` record receives its role cue; an exact unattributed
record synthesized as Narrator receives `Unknown`. A true Narrator route
receives no fallback-role cue. Malformed or inconsistent producer data disables
the manifest rather than inferring authority from display text.

The narrow mode also supports explicit live-fallback decisions and live TTS
when a named role is using Narrator without its own resolved voice. An
unattributed live role can receive `Unknown`; a route with its own resolved
voice does not receive a fallback cue. Source-audio and authorized omission
routes are skipped, so announcements cannot talk over game-owned dialogue.

In either mode the trace source is `live-accessibility-announcement` and the
artifact state is `speaker-announcement-v1`. The cue plays before the already
selected dialogue route, has its own route/outcome timeline stages, and is never
stored as generated story audio. It does not seal the dialogue or dispatch auto
advance; the one canonical dialogue chunk remains the only completion
considered by the reader. Consecutive lines from the same speaker and later
chunks in one dialogue do not repeat it. A cue is committed as heard only after successful playback. Interrupted or
failed cues remain eligible on the next playable line; concurrently prepared
same-speaker cues recheck the committed state before playing. An unavailable
optional announcement reports its configuration error and leaves dialogue
playback available.

`GeneratedAudioFallbackBackend` is an internal route selector/player, not an
exported backend API. All repository callers use `prepare_route()` and
`play_route()`; it therefore has no payload-only `prepare()`/`play()` facade and
does not mirror outcomes through mutable `last_*` fields. No deprecated alias is
kept because this wrapper was never an application entry point or documented
extension interface. The individual live TTS backends now provide typed
`prepare_playback()`/`play_prepared()` calls. Their legacy payload methods and
mutable metrics are retained only as the deprecated external compatibility
facade described in [Synthesis and rendering](synthesis-rendering.md); the
controller and replay paths do not consume them.

Real driver underrun, device-stop latency, game-audio alignment, focus/key
confirmation, and long-soak behavior remain hardware acceptance gates rather
than software unit-test claims.
