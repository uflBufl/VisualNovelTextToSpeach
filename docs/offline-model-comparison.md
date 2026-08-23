# Offline voice-model comparison

VNTTS compares offline renderers on one checksum-bound corpus before a model is
allowed into bulk pregeneration. The comparison does not open an audio device,
change the authoring default, mutate a workspace, review a line, or regenerate
an approved WAV.

The first supported three-way bake-off is:

- `moss-tts`: MOSS Local 4B v1.5 MLX int8, the current authoring baseline;
- `moss-tts-delay`: MOSS Delay 8B v1.5 through its upstream
  PyTorch/Transformers API;
- `coqui-xtts`: Coqui XTTS v2 through the existing typed VNTTS renderer.

These are deliberately different backend identities. Their caches, model IDs,
runtimes, diagnostics and reports cannot be substituted for one another.
MOSS uses the shared benchmark seed. XTTS does not expose equivalent stable
seeded generation, so its request uses `seed=null` and its report records both
`requested_shared_seed` and `seed_policy=unsupported`.

## Install the runtimes

The existing MOSS Local runtime remains:

```sh
uv sync --project backends/moss-tts
```

Install the new Delay runtime separately:

```sh
uv sync --project backends/moss-tts-delay
```

Upstream currently selects CUDA when available and otherwise CPU. It does not
advertise an MPS path. The 8B model is therefore intended for a sufficiently
large CUDA host; a CPU-only Apple Silicon run is a compatibility path, not a
practical throughput claim. Keep `require_cuda: true` on the Delay variant: the
isolated backend then rejects a non-CUDA host before downloading or loading the
8B weights. Model weights are downloaded only when the backend is first
constructed on a qualifying host.

XTTS is installed with the main project. XTTS v2 is distributed under the
Coqui Public Model License (CPML). Read and accept those terms yourself before
changing `terms_accepted` from `false` to `true` in a private copy of
[`samples/offline-model-variants.example.json`](../samples/offline-model-variants.example.json).
VNTTS refuses to construct XTTS when that explicit assertion is absent; it
does not infer consent from another setting or environment variable.

## Build the exact failure corpus and run

Use the immutable queue and authoritative state from the current workspace:

```sh
uv run --no-sync vntts-benchmark-models \
  --queue /absolute/workspace/queue.jsonl \
  --state /absolute/workspace/generated-audio/generation-state.json \
  --manifest /absolute/workspace/inputs/voice/manifest.json \
  --narrator-character Centurion \
  --models /absolute/private/model-variants.json \
  --output /absolute/new/model-comparison
```

The state-backed corpus includes every unresolved item previously attempted by
MOSS, then round-robin samples accepted/generated through Pocket after a MOSS
attempt and technically valid MOSS controls. It binds exact queue and state
SHA-256 values, line/text identities, the current exact manifest-resolved
synthesis voice, the prior synthesis voice/provider/status/failure kind and a
canonical digest of each source state item. Narration requires an explicit
manifest character such as `Centurion`; an unresolved source-reference binding
or model voice fails before either backend is constructed rather than silently
falling back to a default speaker.
Use `--pocket-samples` and `--control-samples` to change only the two bounded
control groups; failures are never sampled away.

Each model is rendered with `BYPASS`, so the comparison cannot be satisfied by
an old Local or Delay waveform. A model directory is published only after all
of its samples complete. A cancelled/limited/error result publishes neither a
partial report nor a partial WAV directory.

Each report records exact WAV SHA-256, sample rate/count, duration, peak,
speech-silence measurements, first-PCM and total render timing, real-time
factor, request/result seed policy, generation profile, backend/model identity
and Python or isolated-worker module provenance. Isolated workers also report
their platform, machine, selected device and, on inspectable CUDA runtimes, the
CUDA runtime, device name, total memory and compute capability. Compare timing
or resource claims only when this hardware provenance is compatible.

## Decision gate

Automatic success is necessary but insufficient. Compare only a small
stratified blind subset after all candidates finish the exact corpus. Listen
for speaker identity, exact words, pronunciation, pacing, pauses, repetitions
and contamination. Keep MOSS Local as the authoring default until another
candidate has a lower technical failure rate, acceptable memory/throughput and
non-inferior human quality. Do not regenerate already approved WAVs as part of
the model bake-off.

The upstream architecture and API rationale are documented in the official
[MOSS-TTS model card](https://github.com/OpenMOSS/MOSS-TTS/blob/main/docs/moss_tts_model_card.md).

## Initial local smoke evidence

On 2026-08-23, both installed local candidates rendered the same exact failed
Rhiannon identity `reverse1999:314605:9:1d0f968d85af2125` ("That's ... That's
what Mam told me.") from the current workspace manifest without playback:

| Backend | Outcome | Duration | Wall time | RTF | WAV SHA-256 |
|---|---|---:|---:|---:|---|
| MOSS Local 4B MLX int8, expressive, seed 0 | complete | 3.440 s | 3.820 s | 1.110 | `79131c4063370351d8a0d276e3bbc1ce5f0f9504ae4c6845a876eb6bfd806aa4` |
| Coqui XTTS v2, stable, no seed | complete | 3.425 s | 4.600 s | 1.343 | `7e3ba67dd89df222c3ab7a0a75625e34fb6b531d7e8b9a62638a66d0ea96392c` |

This is a wiring smoke, not a quality or failure-rate verdict. The exact
state-backed builder selected 46 current lines: 22 unresolved MOSS failures,
12 MOSS-to-Pocket recoveries and 12 MOSS controls. Delay 8B weights were not
downloaded or run on the CPU-only PyTorch runtime, so the three-way corpus gate
remains open.

## Full local 46-line comparison

The first full local attempt on 2026-08-23 is retained as diagnostic evidence
at `authoring/model-comparisons/offline-local-4b-vs-xtts-20260823-v1`, but it is
not a model verdict. It exposed two comparison-boundary defects: Narrator was
not explicitly bound to Centurion for MOSS, while unresolved synthetic state
voice labels could silently reach XTTS's default voice. Its audio and outcome
counts must not be compared as if they represented the same speakers.

The corrected builder resolves every selected queue ID through the exact
captured voice manifest, its source-reference queue bindings and explicit
`--narrator-character Centurion`. It records both the current comparison voice
and the previous state voice, rejects any unresolved identity before model
construction, and publishes the canonical corpus atomically with the reports.
The intermediate `offline-local-4b-vs-xtts-20260823-v2` run established that
identity boundary, but did not retain immutable copies of the reference WAVs.
Keep it as diagnostic evidence rather than the final local verdict.

The authoritative local result is
`authoring/model-comparisons/offline-local-4b-vs-xtts-20260823-v3`. Its corpus
SHA-256 is
`09c4d1dd58c9123e25b49811f1edafd691c4ab8650d9a678a3f07c870021c192`,
and its 12 exact voice-control files have canonical inventory SHA-256
`a41a40b7457e9d5c8e50ef0f646991c3107e4c43380a578ebe3f707705e85192`.
The copied bytes were rehashed after publication, both model reports bind the
same control digest, and both contain the same ordered 46 IDs, character
identities and synthesis voices.

| Backend | Complete | Current authoring silence gate | Render wall sum | Mean RTF |
|---|---:|---:|---:|---:|
| MOSS Local 4B MLX int8 | 44/46 | 43/46 | 201 s | 0.867 |
| XTTS v2, Apple CPU | 46/46 | 46/46 | 233 s | 0.930 |

MOSS reached its typed audio limit on the same two prior MOSS-to-Pocket recovery
lines. Its only additional silence-gate failure was `Soon.`, with 2.16 seconds
of internal silence and a 0.7297 silence ratio. XTTS completed both limited
lines and all 46 outputs passed the current structural silence gate in v3.
XTTS does not accept the shared seed, so its improvement from 44/46 in v2 to
46/46 in v3 is stochastic evidence, not proof of deterministic reliability.
Exact words, speaker identity, pauses, repetition and contamination still
require blind listening. The Apple CPU timing is not a CUDA performance claim.

A preliminary checksum-bound Local4B/XTTS session from the authoritative v3
reports is ready at
`authoring/model-listening/offline-local-4b-vs-xtts-finalists-20260823-v2`.
It contains ten shared COMPLETE trials chosen from asymmetric silence failures,
large duration differences and all three corpus groups. Non-complete outputs
remain automatic technical losses and are not fabricated as blind pairs. The
public session contains no model names; its private key is mode 0600. No CUDA
host is currently available, so use this `0/10` Local4B/XTTS session to select
the best locally runnable candidate instead of blocking on Delay 8B. If Delay
8B becomes available later, compare it only against that local winner in a new
bounded follow-up. The earlier `v1` session is bound to the pre-snapshot v2
reports and must not replace this session.
