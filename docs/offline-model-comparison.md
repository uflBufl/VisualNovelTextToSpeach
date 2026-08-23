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
practical throughput claim. Model weights are downloaded only when the backend
is first constructed.

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
  --models /absolute/private/model-variants.json \
  --output /absolute/new/model-comparison
```

The state-backed corpus includes every unresolved item previously attempted by
MOSS, then round-robin samples accepted/generated through Pocket after a MOSS
attempt and technically valid MOSS controls. It binds exact queue and state
SHA-256 values, line/text identities, the selected synthesis voice, the prior
provider/status/failure kind and a canonical digest of each source state item.
Use `--pocket-samples` and `--control-samples` to change only the two bounded
control groups; failures are never sampled away.

Each model is rendered with `BYPASS`, so the comparison cannot be satisfied by
an old Local or Delay waveform. A model directory is published only after all
of its samples complete. A cancelled/limited/error result publishes neither a
partial report nor a partial WAV directory.

Each report records exact WAV SHA-256, sample rate/count, duration, peak,
speech-silence measurements, first-PCM and total render timing, real-time
factor, request/result seed policy, generation profile, backend/model identity
and Python or isolated-worker module provenance.

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
