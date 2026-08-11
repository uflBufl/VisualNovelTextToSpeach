# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Live mode

### P0 - Prototype streaming Pocket TTS

- [ ] Benchmark Kamuta, Fatutu, and Selone references against Chatterbox Nano:
      model startup, voice conditioning, first-audio latency, realtime factor,
      speaker similarity, artifacts, RAM, and CPU use.
- [ ] Keep Pocket TTS experimental until its voice similarity and 30-minute live
      soak test pass; only then consider making it the live-mode default.

### P0 - Import Reverse: 1999 NPC voices

- [ ] Use Selone as the first assisted-import validation case, then apply the
      same workflow to all unresolved story NPCs.

### P1 - Reduce remaining speech latency

- [ ] Evaluate MLX-Audio on Apple Silicon if Pocket TTS voice similarity is not
      sufficient, and evaluate F5-TTS/TensorRT for the Windows NVIDIA build.

### P0 - Validate performance and resilience

- [ ] Validate the live-mode acceptance targets on supported hardware:
  - No audio underruns or buzzing during a 30-minute live session.
  - Start a cached-voice sentence within 2 seconds on the supported CPU target
    and within 750 ms on the supported CUDA target.
  - Start an already-visible second sentence within 300 ms of the first ending.
  - Never speak or advance stale OCR generations.
- [ ] Use the existing `pynput` controller for the cross-platform prototype, then
      use native Windows `SendInput` for the Windows build and Quartz events on
      macOS after verifying Accessibility permission.
- [ ] Add an emergency stop action and ensure pause, clear queue, focus loss, OCR
      uncertainty, and application shutdown cancel pending input immediately.

### P1 - Benchmark remaining backends and hardware

- [ ] Benchmark XTTS and Chatterbox Nano on the target Windows CPU and CUDA
      hardware; compare first-audio latency, realtime factor, speaker similarity,
      hallucinations, RAM/VRAM, and package size.
- [ ] Prototype Chatterbox Turbo for English CUDA voice cloning and compare it
      with the existing XTTS and Chatterbox Nano measurements.
- [ ] Keep F5-TTS as a GPU quality/performance comparison, not the initial live
      backend, because its strongest published latency path depends on
      TensorRT-LLM-class GPU deployment.

### P1 - Evaluate OCR replacement only after pipeline optimization

- [ ] Benchmark the optimized Tesseract path before adding another OCR runtime.
- [ ] If changed-frame OCR still exceeds the latency target, prototype RapidOCR
      with ONNX Runtime behind an OCR-backend interface and compare speaker-name
      accuracy, dialogue accuracy, CPU use, package size, and Windows deployment.

### P1 - Live-mode integration and safety tests

- [ ] Add deterministic coverage for emergency cancellation during every
      pipeline stage.
- [ ] Add real Windows and macOS soak tests covering CPU and GPU speech, animated
      scenes, rapid manual advancement, and application shutdown during every
      pipeline stage.

## Windows application

### P2 - Windows distribution

- [ ] Produce a standalone Windows build on Windows.
  - [ ] Build on Windows and confirm Python, Qt, Tesseract, English language
        data, and eSpeak-NG are present in the portable artifact.
  - [ ] Verify operation without Python, uv, Tesseract, or development tools installed.

- [ ] Create and sign a Windows installer.
  - Add Start Menu shortcuts, optional startup, upgrades, and clean uninstall support.
  - Preserve downloaded models and user settings during application upgrades.
  - Sign the executable and installer for public distribution.

- [ ] Add Windows compatibility and release testing.
  - Cover Windows 11, common GPU vendors, multiple displays, and DPI scaling.
  - Cover windowed, borderless, normal-user, and elevated game processes.
  - Run installation and OCR-to-speech smoke tests on a clean Windows machine.
