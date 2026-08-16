# Device-independent speech rendering

`vntts.synthesis` defines the boundary between waveform generation and audio
device playback. MOSS-TTS and Pocket TTS implement this boundary; Chatterbox
Nano and XTTS still use their legacy prepared-speech paths.

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
optional deterministic seed. Pocket currently exposes only its `default`
profile and rejects a non-null seed instead of silently ignoring it. Pocket's
model-native token and duration limits are represented as `None`; MOSS reports
its explicit text-derived safety limits.

MOSS live playback, voice preview through `speak()`, warm-up, and the benchmark
all consume the same renderer. MOSS playback drains chunks into its bounded
producer/consumer queue. Pocket playback consumes its renderer's natural chunks
but aggregates a 250 ms lead before the first device write, preserving its live
underrun protection without delaying direct-render access to first PCM.
`stop()`, a false playback guard, or the request's cancellation source stops
rendering and signals cooperative model cancellation where the engine supports
it.

Ahead-of-time callers should consume `result.pcm` and `result.sample_rate`
directly and write their WAV from those fields. They should not provide a fake,
capture, or discard audio output to obtain generated samples.
