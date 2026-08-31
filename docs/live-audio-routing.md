# Typed live audio routing

Live dialogue routing is a two-phase contract. `prepare_route()` returns one
immutable decision: `SourceAudioRoute`, `GeneratedAudioRoute`,
`LiveFallbackRoute`, or `LiveTTSRoute`. Each decision binds its exact payload,
line identity, preflight state, fallback reason, source trace, and preparation timing. A later
`play_route()` returns a route-local `PlaybackOutcome`; controller correctness
does not depend on mutable backend `last_*` fields. The outcome keeps the
decision's effective source, synthesis and first-audio timing, cache source,
playback timing, underrun state, and completion status together, so concurrent
preparation cannot mix metrics from different chunks.

The selector keeps the established precedence:

1. a manual named-character override, explicit force-live Narrator setting, or
   `live-tts-only` policy selects live TTS;
2. active live mode with `prefer-game-audio` and a resolved declared-available
   source selects game pass-through only when that source is a complete reading
   or its semantic completeness is still unknown;
3. a checksum-bound source proven to be a partial cue waits for that game-owned
   cue plus a 350 ms post-roll, then plays the full generated/live reading;
4. `prefer-game-audio` or `prefer-generated` may select a verified generated
   artifact at speed `1.0`; and
5. remaining exact, normalized, ambiguous, missing, unsafe, or policy-skipped
   cases use live TTS with an explicit fallback reason.

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

The 2026-08-29 Character Story trace exposed both sides of this boundary.
`reverse1999:314601:16` was spoken as two live Rhiannon chunks of 99 and 33
characters because no generated manifest entry exists. The next long Hotelier
line, `reverse1999:314601:20`, does have an approved generated entry, but a lost
nameplate made its first 91 characters look like unmatched Narrator text. The
speaker-aware canonical-prefix gate now turns the first case into one complete
live line and the second into the approved generated route.

An OCR body consisting exactly of `...` or `…` is an intentional silent
dialogue, not background noise and not pronounceable text. OCR cleanup preserves
it and the incremental tracker commits it without scheduling speech. In
sequence-owned `audio-auto`, the stronger authority is the unique immediate
line-less silent successor after one application-owned key dispatch: a focused,
changed and settled dialogue frame may confirm that event without depending on
OCR's ability to read three dots. The same focus-checked auto-advance gate may
then continue. Other punctuation-only glyphs remain filtered.

Playback outcomes are `completed`, `interrupted`, `failed`, or
`passthrough-unobserved`. Generated playback alone owns the local output device.
Stopping source pass-through cancels only its completion timer. A guard or stop
before output produces no first-PCM observation; interruption or failure blocks
auto advance for that OCR generation and never seals it. A source route without
observable completion is sealed as the exact line but explicitly blocks auto
advance. It never falls through to live TTS merely to obtain a completion
timer: the game has already spoken that line, so such a fallback would create
audible duplicate dialogue. Player exceptions are recorded as a chunk-bound
failed outcome before the normalized playback error is surfaced.

Generated playback must not use `sounddevice.play()`. That convenience API owns
process-global stream state, while Pocket and other live backends use explicit
`OutputStream` instances. Mixing a 24 kHz Pocket route with a 48 kHz MOSS route
through those two ownership models produced live-only time stretching even
though the published WAVs were correct. In the 2026-08-30 Rhiannon run,
`reverse1999:314601:6` was a valid 4.64 second WAV but its route reported 8.281
seconds of playback; `reverse1999:314601:8` was 6.16 seconds but reported 11.028
seconds. The preceding 24 kHz Pocket WAV differed from its expected duration by
only 0.214 seconds, and direct playback of both 48 kHz files was confirmed
normal.

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
The exact chapter `314601` audit measured all 15 source WEMs at only
0.446-1.422 seconds. Local Whisper controls on three long lines produced
`Please allow me.`, `That'll be an extra charge.`, and `It's all too much!`,
confirming that these are short character cues rather than full readings. The
extractor therefore publishes raw sample-count duration and media SHA-256
separately from a semantic-completeness verdict. A duration-impossible full
reading can be proven `partial`; plausible duration alone stays `unknown` and
cannot authorize timed auto advance. Only a `full` verdict may use source
completion as the final audible route.

Route, voice-resolution, generation-start, first-PCM, completion, outcome, and
suppression timeline records are keyed by chunk ID. Duplicate reports for the
same stage/chunk merge, while multiple chunks in one OCR generation remain
distinct. In sequence-audio modes, one cursor event may complete canonical
playback once; later prepared or queued audio for that same event is suppressed
until an explicit reanchor or a transition gives the cursor a new event. Live
synthesis continues to use the backend's existing cache policy; source and
generated routes bypass live synthesis caches.

Speaker-change announcements are an optional layer above this selector and are
disabled by default. `all-speakers` is the broad accessibility mode: for the
first chunk after any visible speaker changes, the controller prepares a
separate Narrator `LiveTTSRoute`; unattributed `???` uses the spoken name
Narrator. `narrator-fallback-roles` is narrower. The generated-audio loader
retains and validates each lossless producer record, and only a verified
`missing_voice_to_narrator` route receives its original named-role cue. A true
Narrator/Centurion route receives no cue. An exact `???` record whose preserved
source speaker is unattributed and whose requested/effective synthesis role is
Narrator receives the cue `Unknown`. A malformed or inconsistent fallback
record disables the generated manifest instead of guessing from display text.

In either mode the trace source is `live-accessibility-announcement` and the
artifact state is `speaker-announcement-v1`. The cue plays before the already
selected dialogue route, has its own route/outcome timeline stages, and is never
stored as generated story audio. It does not seal the dialogue or dispatch auto
advance; the one canonical dialogue chunk remains the only completion
considered by the reader. Consecutive lines from the same speaker and later
chunks in one dialogue do not repeat it. Original game-audio and unbound live
TTS routes skip the narrow announcement mode, so it cannot invent fallback
authority or talk over audio that the game has already started.

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
