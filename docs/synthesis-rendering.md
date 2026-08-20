# Device-independent speech rendering

## Restart-only runtime identity

Speech backend, model, language, narrator reference and voice-manifest identity
are fixed once a backend has loaded. Saving different values persists the next
startup configuration, but the active controller retains its current runtime
identity and applies only safe live settings such as volume, rate and generation
profile. The UI reports both facts explicitly: the new settings were saved and
the current session still uses the previous backend. A future hot-swap must
construct and health-check a replacement off to the side, then atomically swap
it or keep the old runtime unchanged.

## Isolated backend workers

Pocket TTS, Chatterbox Nano and MOSS-TTS execute in separate worker processes
launched by their own locked environment's Python interpreter. The desktop
process owns capture, routing and audio-device playback; the worker owns model
loading, conditioning caches and waveform generation. A length-prefixed JSON
protocol carries health, prime and live-mode commands plus typed render
requests. Float32 PCM chunks travel as binary frame payloads, followed by the
same completion, limits, timing and diagnostic result used by in-process
renderers.

Startup is fail closed. Before reporting health, the worker verifies the file
origin and version of each backend's ABI-sensitive modules: NumPy and Torch for
Pocket, NumPy/Torch/Transformers for Chatterbox, and NumPy/MLX Core for MOSS.
Every origin must be beneath that backend environment's `site-packages`.
Missing runtimes, mixed imports, an unexpected interpreter, malformed frames or
worker exit prevent the backend from becoming ready. Cancellation terminates
the exact worker process, which also makes non-cooperative model calls bounded;
a later request creates a fresh worker and repeats the provenance gate. App
shutdown sends a graceful command and falls back to terminate/kill.

The startup wait is bounded to 30 minutes so a first model download can finish,
but it polls cancellation instead of sleeping for that entire interval. Local
2026-08-20 smoke evidence loaded and rendered short PCM through isolated Pocket,
Chatterbox and MOSS workers with every reported module origin under the selected
runtime. The MOSS TTS snapshot (`4,864,630,369` bytes) and Audio Tokenizer
snapshot (`2,397,008,800` bytes) were accepted only after their SHA-256 digests
matched Hugging Face metadata. An offline Apple Silicon smoke then returned a
complete 48 kHz stereo render; stopping a separate active generation returned a
typed `cancelled` result and left no render thread alive.

The parent streams worker PCM through its existing output device and applies
the configured volume there. Settings that change backend/model/language/voice
identity remain restart-only, so no worker is silently repurposed across a
different persisted runtime configuration.

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

The upstream MOSS-TTS Local Transformer v1.5 model card describes the model as
sensitive to decoding parameters and recommends `audio_temperature=1.7`,
`audio_top_p=0.8`, `audio_top_k=25`, and
`audio_repetition_penalty=1.0`. VNTTS deliberately exposes a conservative grid:

- `stable`: temperature 0.8;
- `natural`: temperature 1.2;
- `expressive`: temperature 1.7, matching the upstream Local v1.5 default;
- every profile retains upstream top-p 0.8, top-k 25, and repetition penalty
  1.0.

VNTTS also supplies the explicit language name, including `English`, as
recommended for v1.5. A production default must be selected with a fixed-text,
fixed-seed listening comparison rather than inferred from the profile name.
Lower temperature narrows sampling but is not a general speech-rate control.
See the upstream
[MOSS-TTS model card](https://github.com/OpenMOSS/MOSS-TTS/blob/main/docs/moss_tts_model_card.md)
and
[Local v1.5 reference application](https://github.com/OpenMOSS/MOSS-TTS/blob/main/clis/moss_tts_local_v1.5_app.py).

Token-level duration control is distinct from the missed-EOS safety limit.
Normal VNTTS rendering leaves expected duration tokens unset and lets the model
finish naturally; the text-derived `max_tokens` and `max_audio_seconds` only
bound failure. Upstream estimates about 12.5 audio tokens per second, but a
forced target can redistribute surplus duration into cadence or pauses. Use it
only for an explicit duration requirement and compare it against an otherwise
identical uncontrolled render. The first-chunk frame count and streaming
interval affect delivery latency and chunking, not the intended speaking rate.

Voice cloning conditions on one configured reference, currently the first
reference for that character. Use a clean, single-speaker segment in the target
language with natural cadence and no music, sound effects, reverb,
code-switching, or long pauses. The objective PCM preflight detects format,
silence, clipping, low signal and DC offset, but cannot certify language,
speaker identity, background contamination, or suitability of the inherited
style. Those remain manual acceptance gates. Although v1.5 improves
long-reference/short-text cloning, it does not make a contaminated reference a
safe production prompt.

Generated-speech silence analysis is versioned independently from the state
schema. Version 1 accidentally compared raw signed PCM16 amplitudes with a
normalized dBFS threshold, so quiet but nonzero room tone could hide audible
pauses. Version 2 divides PCM16 samples by 32768 before applying the existing
80 ms / -45 dBFS analysis and records `analysis_version: 2` with every new
success or typed silence failure. Existing state without the field remains
readable and checksum-valid through the exact legacy calculation. For review
only, the workbench remeasures such legacy WAVs from one digest-bound byte
snapshot using version 2; this adds attention flags but never changes approval
authority.

One/two-word MOSS hesitation text retains a strict three-second limit. Longer
text receives a 90-word-per-minute allowance plus 2.5 seconds for lead/tail
cadence, still capped at 20 seconds. Both limits remain in persistent cache
identity. Authoring normalizes typed frames-by-one/two-channel PCM to finite
mono before writing; it never flattens channels into the time axis.

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

## Unattributed speaker policy

The exact speaker label `???` keeps its source identity while VNTTS checks for
verified original or generated audio. If neither artifact route is available
and synthesis is required, VNTTS assigns the Narrator voice automatically. It
does not open the unknown-speaker prompt for this exact label. Other unknown,
named speakers still require the normal voice-assignment decision.

Authoring queue planning and legacy queue execution apply the same rule: a
source `speaker: "???"` is rendered with the workspace's configured Narrator
reference, even when an older queue carries a different `voice_character`
fallback. The queue's source speaker remains `???` so provenance and story
matching are not rewritten.

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
