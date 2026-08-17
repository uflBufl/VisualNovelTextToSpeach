# Device-independent speech rendering

`vntts.synthesis` defines the boundary between waveform generation and audio
device playback. MOSS-TTS, Pocket TTS, Chatterbox Nano, and XTTS implement this
boundary.

Call a rendering backend's `render()` method with a `SynthesisRequest`. The
request identifies every input that can intentionally change the waveform:
voice, text, seed, generation profile, cancellation source, and cache policy.
Rendering returns a `SynthesisChunkStream`; iterating it yields float32 PCM
chunks shaped as frames by channels without importing or opening an output
device. After exhaustion, `stream.result` contains the concatenated PCM, sample
rate, completion state, generation limits, first-chunk and total timing, and
cache/profile diagnostics.

```python
from vntts.synthesis import SynthesisCachePolicy, SynthesisRequest

stream = backend.render(
    SynthesisRequest(
        voice="Rhiannon",
        text="The tide is turning.",
        seed=7,
        generation_profile="stable",
        cancellation=cancel_event,
        cache_policy=SynthesisCachePolicy.BYPASS,
    )
)
result = stream.collect()
```

Cache policies have deliberately different authoring semantics:

- `USE` reads and writes memory and persistent speech caches.
- `REFRESH` skips reads and writes a completed replacement waveform.
- `BYPASS` neither reads nor writes generated speech caches. Use it for
  retryable authoring attempts whose seed or quality decision is tracked by the
  authoring job itself.

Only `complete` renders enter the speech caches. Cancelled and text-length
limited PCM remains available in the typed result for diagnostics but is not
reused. Voice-conditioning caches (MOSS prompt codes and Pocket voice states)
are independent and remain enabled for all three policies because they do not
represent a generated attempt.

MOSS profiles are `stable`, `natural`, and `expressive`, and MOSS accepts an
optional deterministic seed. Pocket and Chatterbox currently expose only their
`default` profile and reject a non-null seed instead of silently ignoring it.
Their model-native token and duration limits are represented as `None`; MOSS
reports its explicit text-derived safety limits.

XTTS accepts `configured` to retain the application-level profile and speed, or
an explicit `stable`, `natural`, or `expressive` profile for an isolated render.
It rejects a non-null seed because the wrapped Coqui API does not provide a
stable per-request seed contract. XTTS reports model-native limits as `None`.

MOSS live playback, voice preview through `speak()`, warm-up, and the benchmark
all consume the same renderer. MOSS playback drains chunks into its bounded
producer/consumer queue. Pocket playback consumes its renderer's natural chunks
but aggregates a 250 ms lead before the first device write, preserving its live
underrun protection without delaying direct-render access to first PCM.
`stop()`, a false playback guard, or the request's cancellation source stops
rendering and signals cooperative model cancellation where the engine supports
it.

Live device playback has a second typed boundary. `prepare_playback()` returns
an immutable `PreparedPlayback`, and `play_prepared()` returns its exact
`PlaybackOutcome`. Synthesis time, actual first device write, cache source,
playback time, underrun, interruption, and failure therefore remain bound to
one call even when the next line is prepared concurrently. XTTS and Chatterbox
use per-call stop tokens around their owned output-device call. Pocket and MOSS
bind stop to their owned stream; a MOSS consumer that does not exit after a
bounded abort remains explicitly owned and blocks another playback attempt.

The single-backend benchmark uses only typed `render()` results. Its cold stage
uses `REFRESH`, then verifies an exact `USE` memory hit, clears memory, and
verifies an exact persistent hit when that cache exists. It rejects incomplete
renders, identity/diagnostic mismatches, and an unexpected cache source before
publishing any WAV. It does not open an audio device, so its underrun fields are
deliberately unknown rather than scraped from mutable playback state.

Voice previews and OCR speech tests select the live backend rather than an
artifact-routing wrapper, so XTTS preview generation also crosses this render
boundary before playback.

Ahead-of-time callers should consume `result.pcm` and `result.sample_rate`
directly and write their WAV from those fields. They should not provide a fake,
capture, or discard audio output to obtain generated samples.

The concrete backend `prepare()`, `play()`, `speak()`, and `last_*` attributes
remain only as a deprecated compatibility facade for existing external Python
callers. Repository production paths use the typed methods and do not read the
mutable facade. MOSS preserves its historical synthesis/configuration exception
types through that facade; Pocket keeps its historical `AudioPlaybackError`
normalization. Remove the facade only in a major release after an external API
usage audit and a documented migration window.
