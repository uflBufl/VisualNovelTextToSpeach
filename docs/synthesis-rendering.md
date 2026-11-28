# Device-independent speech rendering

## Runtime identity and isolation

Changing the live backend, model, language, narrator reference or voice
manifest saves the next runtime configuration. The loaded backend retains
its identity until restart. Settings such as volume, speech rate and supported
generation profiles can apply to the current runtime.

Pocket TTS, Chatterbox Nano, MLX MOSS and Qwen use isolated Python workers.
MOSS Delay uses the same worker boundary for offline rendering. The Windows
MOSS Local backend owns an OpenMOSS native server. The app owns capture,
routing and device playback. The speech worker owns model loading,
conditioning and generation.

The Python worker checks its interpreter and the origin and version of required
packages before reporting health. The VNTTS source or bundle root is available
to the worker. Model dependencies come from the selected runtime's locked
environment. Length-prefixed control frames carry typed requests and outcomes,
with binary float32 frame payloads for PCM. Startup and requests have bounded
waits. Cancellation or a request timeout can retire the exact process, and a
subsequent request prepares a fresh worker. See
[Speech engines and voices](speech-engines.md) for setup.

## Render a waveform

Use [`vntts.synthesis`](../vntts/synthesis.py) to produce speech without
opening a device. The request binds the text, voice, profile, seed,
cancellation and cache policy. The stream yields float32 PCM as frames by
channel. Its final result binds PCM, sample rate, completion, limits, cache
source and timing.

```python
from vntts.synthesis import SynthesisCachePolicy, SynthesisRequest

result = backend.render(
    SynthesisRequest(
        voice="Narrator",
        text="The tide is turning.",
        seed=7,
        generation_profile="stable",
        cancellation=cancel_event,
        cache_policy=SynthesisCachePolicy.BYPASS,
    )
).collect()
```

The example uses a backend that supports the specified seed and profile.
Consult backend capabilities before selecting those controls. For an offline
WAV, consume `result.pcm` and `result.sample_rate` and downmix across the
channel axis before writing mono.

| Cache policy | Behavior |
| --- | --- |
| `USE` | Read and write completed speech in the available caches. |
| `REFRESH` | Skip cache reads and write a completed replacement. |
| `BYPASS` | Skip both speech cache reads and writes, for example in an authoring attempt. |

Cancelled and limited results can retain partial PCM for diagnostics. Only
complete renders enter speech caches. Reference-conditioning caches are
independent of these policies.

## MOSS controls and quality

The MLX profiles use temperatures 0.8, 1.2 and 1.7 for `stable`, `natural`
and `expressive`, with the same top-p, top-k and repetition penalty. Establish
the preferred profile with fixed-text and fixed-seed listening. Temperature
changes sampling. First-chunk size and streaming interval change PCM delivery.
An explicit target duration is a separate experimental control.

The missed-EOS limit allows at most three seconds for one- and two-word
lines. Longer text gets a bounded cadence allowance capped at 20 seconds.
Limited speech is reported separately and kept out of audio caches. The
reference chosen by an individual render depends on the backend and voice
binding. Explicit selected-reference authority must be preserved.
[Reference selection](authoring-reference-selection.md) covers immutable
manual choices.

Speech silence analysis v2 scales integer PCM into normalized amplitude before
measurement. Legacy state remains readable with its original calculation and
review may remeasure the same WAV bytes without altering approval.
[Review-attention silence policy](review-attention-silence-policy.md) describes
the difference between publication validation and listening prompts.

## Play or preview speech

`prepare_playback()` binds an immutable `PreparedPlayback` to a line.
`play_prepared()` returns its call-bound `PlaybackOutcome`, including the
status, timing and failure category. VNTTS streams synthesized PCM or transfers
prepared PCM to the native audio worker. Announcements and voice previews use
the same application playback boundary.

Original audio, ordered dialogue, overrides and auto advance are owned by
[Live audio routing](live-audio-routing.md). The exact role `???` retains its
original identity during artifact lookup. If synthesis is needed, it uses
Narrator without prompting to bind that role.

The typed APIs are the supported path for application orchestration. Concrete
`prepare()`, `play()`, `speak()` and mutable `last_*` attributes remain for
external Python compatibility. Removing this facade requires a major release,
a downstream usage audit and a documented migration window. See the
[facade audit](deprecated-speech-facade-audit.md) for the previous migration.

For reference checks, render benchmarking and device probes, see
[CLI and diagnostics](diagnostics.md#reference-and-render-checks).
