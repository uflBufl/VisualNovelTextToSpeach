# Opt-in native MOSS timing build

## Windows adaptive runtime

This is an **opt-in developer runtime**, not normal app setup. One Windows x64
Vulkan build selects its auxiliary CPU worker count and Local audio-frame GPU
placement at process start. It never switches a loaded model in place: fallback
is an owned-server restart without `--local-gpu`. The persistent CPU pool stays
OFF and remains a separate experiment.

### Before starting

Use Windows x64, an up-to-date VNTTS checkout, uv, a Vulkan-capable GPU driver
and the [Microsoft Visual C++ x64 runtime](https://aka.ms/vc14/vc_redist.x64.exe).
Run commands from the project folder. Close VNTTS and other model applications.

You need the existing MOSS Local 1.5 GGUF pair (about 9.1 GB) and a saved game
Narrator reference. If ordinary MOSS speech already works, use those same files.
If not, complete MOSS setup and save a game narrator in `uv run vntts-app`,
then **close the app**. An MLX directory or Pocket preset is not sufficient.
This comparison does not download weights or silently substitute a voice.

### Download

Open the [VNTTS adaptive Windows runtime](https://github.com/AlexRedby/openmoss/actions/workflows/vntts-adaptive-windows.yml)
workflow in the OpenMOSS fork.
From one successful run, save `moss-native-timing-adaptive-windows-x64.zip`.
Keep the exact name and do not mix it with older `timing-local-gpu` or `aux8`
artifacts. Its `VNTTS-BUILD.json` records the pinned fork, upstream and llama.cpp
commits plus the runtime-control bounds. It must also report `ggml_native: false`;
the build deliberately targets portable AVX2 instead of the instruction set of
whichever GitHub runner happened to compile it.

Before loading weights, inspect the machine-readable contract:

```powershell
.\moss-tts-server.exe --capabilities-json
```

It emits schema `vntts.openmoss.capabilities`, version `1`, and booleans
`vulkan_optional`, `vulkan_available`, `local_gpu`, `aux_cpu_threads`, plus
default/min/max thread fields. `local_gpu` means the binary compiled the path;
`vulkan_available` is no-model backend-device enumeration for this machine.
The adaptive Python contract is already present, but the pinned v0.3.0 runtime
remains legacy until an immutable adaptive runtime release is available.

After a successful load, `/info.placement` reports actual `backbone`, `device`,
`local`, `auxiliary`, `gpu_layers`, and `aux_cpu_threads`. `device` is the
selected hardware description, capped at 128 characters, and is empty for CPU-only
placement.

### Real Windows qualification

After downloading the successful Actions artifact to the default Downloads
folder, close VNTTS and run this once from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\qualify-moss-adaptive-windows.ps1
```

The command verifies that the artifact was built from the expected OpenMOSS commit,
reuses the installed GGUF pair and saved Narrator reference, then renders the same
seven samples with CPU Local/2, 4, 6 and 8 workers plus adaptive Local GPU/8
workers. A checksum-bound `limited` result records the production safety cap and
does not stop the remaining configurations; it is marked ineligible for speed
conclusions. The GPU run automatically selects a second usable saved game voice and
records cold/warm timings before and after the voice change. It also cancels one
uncached request, confirms that process exited and completes a fresh render in a
new owned process. Any failed or malformed render, inconsistent bounded outcome,
capability/placement mismatch, different CPU WAV hash, or surviving server fails
the qualification.
Reports separate native phases, output/raw WAV validation, and final archive
publication time; unavailable required timing is a failure, not zero.

On success, `moss-adaptive-qualification-*.zip` and its `.sha256` and `.json`
receipt stay in Downloads. The archive declares that it contains generated voice
audio, timings and resource measurements for the final GPU listening check; JSON
reports contain basenames and hashes instead of local absolute paths. On failure,
the unarchived output folder remains with `qualification.json` set to
`qualified: false`. The command does not change saved app settings or install the
candidate runtime.

### Promote a qualified runtime

Do not download and upload the runtime again. In the OpenMOSS repository, run
the **Promote qualified VNTTS runtime** workflow with:

- `build_run_id`: the numeric ID in the qualified adaptive build's Actions URL;
- `release_tag`: a new tag such as `v0.3.0-vntts-timing-2`;
- `runtime_sha256`: `runtime_archive.sha256` from the qualification archive's
  `qualification.json`.

The workflow accepts only a successful adaptive push build from `main`, downloads
that exact retained Actions artifact, verifies its source manifest and both
checksums, then creates the release with only the runtime ZIP and checksum. It
refuses an existing tag or release and never replaces published assets. Actions
artifacts expire after 30 days, so promote the qualified run before then.

### Run

The native server accepts these process-start controls:

```text
--aux-cpu --aux-cpu-threads N    N is 1..16; default is 4
--aux-cpu --local-gpu            Local decoder only, on the selected Vulkan device
```

`--aux-cpu-threads` other than the default and `--local-gpu` both require
`--aux-cpu`; Local GPU also requires a GPU backbone (`--n-gpu-layers` must not
be `0`). Bad startup controls print exactly
`VNTTS_STARTUP_FAILURE_JSON={"category":"..."}`. Eligible Local GPU categories
are `local_gpu`, `vulkan_device`, and `vulkan_allocation`. Failures while
creating or loading the optional Local GPU owner are `local_gpu`, so the retry
keeps the backbone placement and moves only Local to CPU; model corruption and
ordinary request failures are not classified as fallback candidates.

The existing two-artifact `moss_native_compare` workflow intentionally remains
for the legacy runtime until the immutable adaptive runtime is released. Do not
feed it this single adaptive artifact yet. The native contract is safe to inspect with
`--version`, `--help`, and `--capabilities-json` without model weights.

Windows qualification still needs a real Local v1.5 GGUF pair, a Vulkan-capable
device and an owned-process restart test: CPU Local with 4 and 8 workers must
remain byte-identical; Local GPU with 8 workers needs resource, technical and
listening approval. CI compilation and no-model checks do not prove GPU kernels,
VRAM headroom, model ownership or speech quality.

## Native source details

The [VNTTS OpenMOSS fork](https://github.com/AlexRedby/openmoss) is based on
Apache-2.0 openmoss v0.3.0 revision
`bfb1f465e0a86fb5a52bbf93e67ceba4b7d0b4e1`, with llama.cpp
`050ee92d04c2e1f639025786dea701c70e7d4204`. Its commits add timing and
backbone ownership cleanup, without changing sampling, device placement, thread
counts or audio limits. The built server identifies itself as
`0.3.0-vntts-timing1` in `/info` and `--version`.

The **VNTTS adaptive Windows runtime** GitHub Actions workflow in the OpenMOSS
fork builds a separate Windows Vulkan artifact from its exact commit. It checks startup without
loading weights; that is not GPU, performance or audio-quality qualification.
It never publishes a release or changes VNTTS's automatic runtime installer.

For the controlled test, use the comparison command above. The separate
`scripts/run-moss-windows.ps1` launcher opens an interactive app; it is **not**
a prerequisite for the comparison. Do not overwrite the managed runtime folder.

## Experimental Local audio-frame model on GPU

The `timing-adaptive` build compiles both CPU and Local GPU paths. `--local-gpu`
selects the separate Local decoder owner at model load; omitting it preserves the
CPU Local path. The build identifies itself as `0.3.0-vntts-timing1`; the manifest
records the pinned source commits and runtime-control bounds.

This candidate keeps CPU-owned input embeddings and the waveform codec, but
uses a separate GPU owner for the Local transformer, its text head and a copy
of the audio embedding tables. It does not move the whole auxiliary sidecar.
The actual split is reported in startup status and `/info`; a different
`same_reported_compute` value in the comparison is intentional, not a failure.

For the pinned Q8 GGUF pair, the extra GPU weight group is about 205 MiB
(145 MiB Local weights plus 60 MiB audio embeddings). These are static tensor
bytes, not a VRAM-fit guarantee: graph buffers, device allocations, context and
other applications also consume memory. Keep `-CodecOnGpu` unset. Do not use
this candidate with a different model architecture or without a GPU.
The separate GPU path checks graph operation support before execution and
returns the backend and unsupported operation name instead of attempting it.

`moss-local-gpu-check.exe` exercises runtime-control validation and routing without model weights.
CI compilation and this check do not qualify GPU kernels, speech quality,
real model ownership, VRAM headroom or speed. Compare fresh WAVs and resources
on Windows before adoption; unlike the CPU pool test, different arithmetic
may produce different WAV bytes without proving an audible regression.

First-use reference encoding remains on CPU. The existing
`reference_encoding_s` measurement is included in comparison phase summaries;
do not attribute that one-time work to audio-frame generation. Missing warm
reference timing remains unavailable, not an inferred zero or proven cache hit.

## Experimental auxiliary CPU pool

The adaptive workflow fixes `OPENMOSS_PERSISTENT_AUX_CPU_POOL=OFF`. A separate
future pool experiment may enable it and identify itself as
`0.3.0-vntts-timing1-pool1`. The auxiliary CPU
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
the exact fork commit, since rebuilds may retain the same diagnostic version.

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
model CPU work. `--aux-cpu-threads N` changes only the separate direct auxiliary
CPU backend (default 4; supported range 1..16), not main-model CPU threads.
Moving the full auxiliary sidecar to GPU remains a separate, unqualified change.

## Third-party notices

The Windows archive includes the x64 Vulkan loader from the pinned SDK runtime
and its original notices in `vulkan-notices`. It does not install drivers or
modify the system runtime; GPU use still needs a compatible installed driver.

The artifact includes openmoss's Apache-2.0 license, llama.cpp's license and
dependency notices. Its server also incorporates cpp-httplib 0.18.7
(Copyright (c) 2025 Yuji Hirose. All rights reserved.) and nlohmann/json 3.12.0
(Copyright (c) 2013-2026 Niels Lohmann), both under the MIT license reproduced
in `LICENSE-cpp-httplib.txt` and `llama-licenses/LICENSE-jsonhpp`.
