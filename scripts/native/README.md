# Opt-in native MOSS timing build

This diagnostic patch is based on Apache-2.0 openmoss v0.3.0,
revision `bfb1f465e0a86fb5a52bbf93e67ceba4b7d0b4e1`, with llama.cpp
`050ee92d04c2e1f639025786dea701c70e7d4204`. It changes timing only, not
sampling, device placement, thread counts or audio limits. Modified sections
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

The artifact includes openmoss's Apache-2.0 license, llama.cpp's license and
dependency notices. Its server also incorporates cpp-httplib 0.18.7
(Copyright (c) 2025 Yuji Hirose. All rights reserved.) and nlohmann/json 3.12.0
(Copyright (c) 2013-2026 Niels Lohmann), both under the MIT license reproduced
in `LICENSE-cpp-httplib.txt` and `llama-licenses/LICENSE-jsonhpp`.
