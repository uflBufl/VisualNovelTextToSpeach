# Speech engines and voices

Choose the live engine under **Speech and voices** in Settings. **Stories**
has a separate generation-engine selector for preparing audio ahead of time.
Restart when Settings requests it after a change to the live model or runtime.
The current engine controls new speech. Prepared recordings keep their own
model and voice.

## Pocket TTS

Pocket TTS is the default live engine. Its built-in voices work without an
account. The portable packaging scripts stage its Python runtime, and a source
checkout can prepare the locked CPU environment with uv when needed.

For voice cloning, select **Prepare voice cloning...** in Settings, Reading
setup or Stories. Complete Hugging Face sign-in and accept the original model
terms in your browser, then continue the download in VNTTS. Setup supports
progress, cancellation and retry and reuses cached files. After preparation
the local model can load without signing in again.

## MOSS-TTS Local v1.5

- **Apple Silicon macOS** uses the isolated MLX engine. A source checkout can
  prepare its locked runtime automatically. The default int8 model and
  tokenizer require several gigabytes on first use. Select a narrator voice
  reference. Use `VNTTS_MOSS_MODEL` for a compatible custom MLX model.
- **Windows x64** uses the OpenMOSS native runtime. Select MOSS-TTS and a
  narrator reference, then approve the runtime and model download when
  prompted. Keep about 9.1 GB for the two Q8 model files plus installation
  headroom. The Microsoft Visual C++ x64 runtime must be installed. VNTTS checks
  the native executable, verifies downloads and reuses them on later launches.
  The default requests Vulkan offload for the backbone and keeps the auxiliary
  model on the CPU. Check the reported placement after the model loads.

For manual Windows setup, extract the matching
[OpenMOSS release](https://github.com/AlexRedby/openmoss/releases/tag/v0.3.0-vntts-timing-2)
with its DLLs and put both
[Local v1.5 Q8 model files](https://huggingface.co/ilintar/moss-tts-local-gguf/tree/main)
in one folder. Run from the repository root with your actual paths:

```powershell
.\scripts\run-moss-windows.ps1 -Server C:\MOSS\moss-tts-server.exe -Model C:\MOSS\moss-tts-local-1.5-q8_0.gguf
```

The script applies overrides for that launch. Use `-GpuLayers 0` for CPU or
reduce the layer count to leave VRAM for the game. `-CodecOnGpu` moves the
entire auxiliary model to the GPU. The combined weights alone exceed 8 GB
before working memory. Use `-Application` to launch an existing portable
executable or `-NarratorReference` to benchmark generation.

## Other engines

- **Chatterbox Nano** uses an isolated source runtime. The first run downloads
  model assets. To prepare a repository-local environment explicitly, use
  `uv sync --project backends/chatterbox-nano --locked`.
- **Qwen TTS** uses the Windows Qwen environment or the Apple Silicon MLX
  environment, with platform-dependent model selection. Use the available
  choices in Settings or Stories.
- **Coqui XTTS v2** requires acceptance of the model terms. The `stable`,
  `natural` and `expressive` profiles control generation. For terminal
  configuration, see [CLI and diagnostics](diagnostics.md#terminal-reading).
- **MOSS Delay** is an offline comparison backend. See
  [Offline model comparison](offline-model-comparison.md) for its test harness.

Source setup can create application-owned locked environments below local app
data. It verifies a replacement before switching and keeps explicitly
configured environments and existing `backends/*/.venv` directories intact.
For available per-backend overrides, see [runtime paths](../vntts/runtime_paths.py).
A packaged app with a missing bundled runtime requires a complete compatible
bundle.

## Assign a live voice

Open **Narrator voice** to audition voices on the same text and assign one to
Narrator or a character. You can enter a character name as it appears in OCR
even when it is absent from an imported manifest. The selected live fallback
voice does not change already prepared recordings. See
[Choose where speech comes from](playing.md#choose-where-speech-comes-from).

Use short, clean, single-speaker references without overlapping speech or
music. Available engines can differ in how they choose and condition on
references. For objective checks and the limits of automated signals, see
[Voice quality gates](voice-quality-gates.md) and the
[reference preflight](diagnostics.md#reference-and-render-checks).
