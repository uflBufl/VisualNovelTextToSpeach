# Opt-in native MOSS timing build

This diagnostic patch is based on Apache-2.0 openmoss v0.3.0,
revision `bfb1f465e0a86fb5a52bbf93e67ceba4b7d0b4e1`, with llama.cpp
`050ee92d04c2e1f639025786dea701c70e7d4204`. Both diagnostic variants add timing and
backbone ownership cleanup, without changing sampling, device placement, thread
counts or audio limits. Modified sections
are marked in the patch. The built server identifies itself as
`0.3.0-vntts-timing1` in `/info` and `--version`.

The **Native MOSS timing build** GitHub Actions workflow builds a separate
Windows Vulkan artifact from these exact revisions. It checks startup without
loading weights; that is not GPU, performance or audio-quality qualification.
It never publishes a release or changes VNTTS's automatic runtime installer.

For a controlled test, extract the entire archive into a separate directory,
keeping its DLLs beside the EXE. Use the existing `scripts/run-moss-windows.ps1`
with `-Server` pointing to that EXE and `-Model` pointing to the existing GGUF.
Leave `-CodecOnGpu` unset on the 8 GB test GPU. Exit that shell afterwards to
discard its runtime overrides. Do not overwrite the managed runtime folder.

## Experimental auxiliary CPU pool

The workflow also produces a separate `timing-aux-pool` archive, identifying
itself as `0.3.0-vntts-timing1-pool1`. Its only additional change is enabling
`-DOPENMOSS_PERSISTENT_AUX_CPU_POOL=ON` (default `OFF`). The auxiliary CPU
backend retains its four workers across graphs instead of creating and joining
them for every graph. Idle workers sleep (`poll=0`); the disposable baseline
uses GGML's default polling policy. The Aux owner detaches and frees the pool
at quiescent teardown. Backbone and GPU backends are unchanged.

Both archives include `moss-aux-pool-check.exe` and its JSON result. This
no-model check compares repeated synthetic CPU graph output bit-for-bit,
exercises abort recovery and destroys/recreates the real Aux owner. It runs
in CI for both variants. Its timings are **not a speech performance result**.

Keep baseline and experiment in separate directories. Adoption still requires
matching speech output codes with identical inputs/seed, safe request
cancellation and shutdown, and improved cold/warm phase timings without worse
idle CPU or RSS on Windows (CPU-only and CPU-auxiliary modes). Neither artifact
is installed automatically, and no user voice approvals are inferred.

Both variants free the raw libllama context and model during `Model` destruction,
after dependent graph owners and before Aux teardown. Context-creation failure
uses the same owner cleanup instead of a separate manual free. This correction
is shared by both variants so it does not confound the pool comparison. Compile
and synthetic pool checks do not establish leak-free real-model load/unload;
that remains a separate qualification. The packaged `VNTTS-BUILD.json` records
the exact patch hash, since rebuilds may retain the same diagnostic version.

## One-command Windows comparison

Extract both artifacts from the **same workflow run** into separate directories,
keeping all DLLs beside each EXE. Close VNTTS first so another model does not
compete for RAM/VRAM. From the project folder, replace the two example paths:

```powershell
uv run --no-sync python -m scripts.moss_native_compare --baseline "C:\native\timing\moss-tts-server.exe" --candidate "C:\native\timing-aux-pool\moss-tts-server.exe" --output "$env:USERPROFILE\Downloads\moss-pool-comparison"
```

This uses the saved narrator reference and existing GGUF, without downloads or
settings changes. Override them with `--reference "C:\voice.wav"` and
`--model "C:\models\model.gguf"` if needed. The output folder must not already
exist. Keep the GPU default (`--gpu-layers -1`) for GPU backbone/CPU auxiliary;
a separate CPU-only run uses `--gpu-layers 0` and a different output folder.

The order is baseline, candidate, candidate, baseline. Each fresh server renders
the same three phrases with seed 1, production native stable sampling and no
synthesis cache: **12 generations total**. Startup and the first request are
reported separately from warm requests. Process-cold does not mean cleared OS
or disk caches. It can take several minutes; Ctrl+C preserves partial results.

Send the resulting `moss-pool-comparison.zip` from Downloads. It includes timings,
available CPU/RSS measurements, input/binary/DLL hashes, reports and generated
WAVs; it excludes weights and the original reference recording. Nothing is
uploaded automatically. Failed/limited cases are not treated as speed gains.
Exact WAV equality is reported, but the native API does not expose codec codes;
neither these measurements nor exit code 0 prove acoustic quality, cancellation
safety, idle CPU behavior or leak-free real-model load/unload.

## Measurements

VNTTS reads optional response headers from non-streaming `/tts` and
`/v1/audio/speech` into `native-speech.json` support events:

| Header | Support field | Scope |
| --- | --- | --- |
| `X-MOSS-Backbone-Seconds` | `gen_backbone_s` | Autoregressive libllama decode, including device synchronization |
| `X-MOSS-Frame-Decoder-Seconds` | `gen_frame_decoder_s` | Local depth transformer, sampling, cache work and readback |
| `X-MOSS-Input-Embedding-Seconds` | `gen_input_embedding_s` | Next-row input embeddings and readback |

These are wall-clock phases, not pure kernel times. They exclude prefill and
the waveform codec. Prefill still combines prompt embeddings and main-model
prefill, so adding it to `gen_backbone_s` is not pure main-model time. The
existing `decode_s` measures waveform codec work. The three new phases need
not sum exactly to `gen_s`: loop bookkeeping and any streaming drain are
outside them. Stock runtimes return no new headers, reported as unavailable,
never zero. No speech text, reference paths or per-token traces are added.

The patch synchronizes libllama before stopping its timers; otherwise device
work can be charged to the following phase when the next output is read.
Auxiliary graph calls already synchronize and return host data.

In this pinned runtime, CPU graph planning defaults to four threads for main
model CPU work, and the separate auxiliary backend also defaults to four. No
thread CLI is exposed. Increasing thread counts or moving the full auxiliary
sidecar to GPU requires a separate measured change; neither is done here.

## Third-party notices

The Windows archive includes the x64 Vulkan loader from the pinned SDK runtime
and its original notices in `vulkan-notices`. It does not install drivers or
modify the system runtime; GPU use still needs a compatible installed driver.

The artifact includes openmoss's Apache-2.0 license, llama.cpp's license and
dependency notices. Its server also incorporates cpp-httplib 0.18.7
(Copyright (c) 2025 Yuji Hirose. All rights reserved.) and nlohmann/json 3.12.0
(Copyright (c) 2013-2026 Niels Lohmann), both under the MIT license reproduced
in `LICENSE-cpp-httplib.txt` and `llama-licenses/LICENSE-jsonhpp`.
