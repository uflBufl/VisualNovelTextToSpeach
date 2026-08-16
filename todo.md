# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Offline authoring and application responsibility split

### P1 - Move generic generation authoring into VNTTS

- [ ] Add an isolated `vntts.authoring` package and separate
      `vntts-pregenerate` entry point; keep long-running generation, review, and
      model-selection workflows out of the primary player interface.
- [ ] Move the generic queue builder, delivery annotations, bulk generation,
      generated-WAV validation, resumable state, approval, and manifest
      publication from `r1999extractor` into `vntts.authoring`.
- [ ] Build generation jobs from versioned story-index and voice-manifest
      fields. Interpret missing source audio as an authoring policy without
      importing Reverse: 1999 modules or understanding game-specific IDs.
- [ ] Move model benchmarking and blind listening, including their UI and
      reports, into the authoring package so production-model decisions are
      owned beside the TTS engines they evaluate.
- [ ] Publish a final verified game pack from the authoring workflow after
      generation and review; keep extracted inputs, generated WAVs, and review
      state in application data rather than the repository.

### P1 - Preserve existing generation work

- [ ] Read and migrate existing `r1999.pregeneration-job`, generation queue,
      generation state, review decisions, and generated-audio manifests without
      deleting, overwriting, or regenerating valid local artifacts.
- [ ] Keep compatibility with current queue IDs, line IDs, text hashes, seeds,
      attempts, and approval state until all existing jobs have been imported
      and resumed successfully in VNTTS.
- [ ] Generate and review the 1,220 `no_audio` patch 3.7 lines with the approved
      `moss-tts-local-transformer-v1.5-mlx` model after their voice references
      are available. Preserve source-audio candidates instead of replacing them
      with generated speech.

### P1 - Make the authoring workbench truthful and actionable

- [ ] Persist structured current-attempt state with the line, speaker, phase,
      attempt number, start time, and latest error. Show it with a live elapsed
      timer so slow generation does not look frozen.
- [ ] Clear stale process metadata on restart and distinguish generation
      running in this window, running in another process, and interrupted jobs.
- [ ] Present generated, failed, skipped, and pending outcomes together, with
      grouped reasons and focused retry or voice-review actions.
- [ ] Add a preflight summary that separates candidate lines into ready,
      missing-reference, recoverable-source-audio, manual-review, and skipped
      sound-effect counts before generation starts.
- [ ] Keep narrator and story selection prominent; collapse rarely changed
      paths behind readiness details and provide a searchable reference chooser
      with Play/Stop, previous/next reference, duration, and recent choices.
- [ ] Give story titles and selection details enough space, explain disabled
      actions, and rename ambiguous line and voice counts.
- [ ] Collapse raw engine output under technical details and promote actionable
      warnings with copy-diagnostics support.
- [ ] Add output-folder and retry-failed actions, friendly job timestamps,
      persisted layout state, keyboard focus coverage, and status cues that do
      not depend on color alone.

## Live mode

### P0 - Repair failures observed in the latest Character Story run

- [ ] Re-run the chapter opening and verify that indexed incomplete prefixes
      are never spoken or auto-advanced. The 2026-08-16 run spoke only 43, 13,
      and 44 characters of the first three 94, 24, and 69-character lines;
      each fragment now resolves as one unique incomplete story prefix and must
      remain pending until the full line or a verified generated route is ready.
- [ ] Validate the explicit Narrator voice dialog: selecting a candidate must
      bypass pregenerated narrator tracks with the chosen live voice, while
      "Use pregenerated narrator tracks when available" must remove that
      override and restore artifact routing.
- [ ] Validate unique-prefix generated-audio routing in the next Character Story
      run. The 2026-08-16 baseline delayed generation start by as much as 11 s
      even though Pocket itself reached first PCM roughly 6-143 ms later; the
      new route must start the verified WAV during the typewriter animation and
      fall back safely for short, ambiguous, or OCR-corrupted prefixes.
- [ ] Repeat the Character Story run after connecting the approved generated
      manifest. The 2026-08-16 run reduced route-level `story-line-no-match` to
      2 of 9 observations, but still produced a partial suffix and the bogus
      speaker `Oe`; verify that generated Rhiannon lines route to local WAVs.
- [ ] Validate the 250 ms Pocket TTS stream prefill on real audio output. The
      2026-08-16 run still recorded 3 underruns with the previous 120 ms lead;
      retain the new underrun count as acceptance evidence.

### P0 - Reproduce and gate the Rhiannon Character Story

- [ ] Capture a deterministic replay corpus from the reported Rhiannon
      Character Story/Anecdote, including Rhiannon, Hotelier, Adar Llwch Gwin
      Fledgling, narration, the short `I, erhm ...` line with transient
      nameplate/background OCR noise, an installed source-audio line, and a
      line that requires live synthesis.
- [ ] Pass a 20-line real-game acceptance run with no stale or duplicate speech,
      no skipped dialogue, no early advances over original audio, and automatic
      progression of every tested dialogue line.

### P0 - Establish a MOSS quality and latency gate

- [ ] Run `vntts-benchmark-tts` on the completed exact Rhiannon replay corpus,
      add generated/original game-audio route timing, and retain the report as
      acceptance evidence. The reported-line regression already covers fresh,
      memory/persistent cache, first PCM, realtime factor, underruns, and the
      missed-EOS safety cap.
- [ ] Run a small blinded listening comparison for Rhiannon speaker similarity,
      pronunciation, prosody, noise, repetitions, and trailing silence. Compare
      the stable generation profile with the current reference codec roundtrip
      and the original extracted line before tuning sampling again.
- [ ] Finish the manual contamination review and blinded comparison of the three
      objectively passing Rhiannon references, then decide whether to keep the
      documented first-reference policy, select another clean reference, or
      build and validate a combined prompt. Objective preflight cannot detect
      music or a second speaker reliably.
- [ ] Validate the selected 4-frame/0.25-second MOSS stream on real audio output
      during the hardware soak. The local discard-sink grid reduced first PCM
      from about 1264 ms to 640 ms at RTF 0.88, but cannot expose driver jitter.
- [ ] Pre-resolve or explicitly approve unknown speakers before live playback so
      a mid-story voice prompt and narrator fallback cannot masquerade as MOSS
      latency or Rhiannon voice quality.

### P1 - Simplify the live speech boundary after the P0 replay passes

- [ ] Split route selection from synthesis/playback. The current
      `GeneratedAudioFallbackBackend` owns original game pass-through, generated
      files, live TTS fallback, voice overrides, and metrics; replace it with a
      typed route decision plus source-specific players.
- [ ] Replace mutable backend `last_*` compatibility metrics with the generation
      timeline now used by diagnostics and support bundles.
- [ ] Document the final routing precedence, source-audio completion contract,
      cache semantics, and auto-advance confirmation behavior in the README and
      game-pack contract.

### P0 - Validate performance and resilience

- [ ] Validate the live-mode acceptance targets on supported hardware:
  - No audio underruns or buzzing during a 30-minute live session.
  - Start a cached-voice sentence within 2 seconds on the supported CPU target
    and within 750 ms on the supported CUDA target.
  - Start an already-visible second sentence within 300 ms of the first ending.
  - Never speak or advance stale OCR generations.

### P1 - Benchmark remaining backends and hardware

- [ ] Complete the authoring workflow's perceptual model gate before adding
      another production speech backend, then integrate only the selected
      winner.
- [ ] Benchmark XTTS and Chatterbox Nano on the target Windows CPU and CUDA
      hardware; compare first-audio latency, realtime factor, speaker similarity,
      hallucinations, RAM/VRAM, and package size.
- [ ] Prototype Chatterbox Turbo for English CUDA voice cloning and compare it
      with the existing XTTS and Chatterbox Nano measurements.
- [ ] Keep F5-TTS as a GPU quality/performance comparison, not the initial live
      backend, because its strongest published latency path depends on
      TensorRT-LLM-class GPU deployment.

### P1 - Evaluate OCR replacement only after pipeline optimization

- [ ] Validate the optional RapidOCR backend in the Windows portable build and
      compare it with Tesseract on the target Windows hardware.

### P1 - Live-mode integration and safety tests

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

- [ ] Run and record Windows compatibility and release qualification evidence.
  - Cover Windows 11, common GPU vendors, multiple displays, and DPI scaling.
  - Cover windowed, borderless, normal-user, and elevated game processes.
  - Cover a 30-minute soak, rapid and manual advancement, and shutdown during
    every live-pipeline stage.
  - Run installation and OCR-to-speech smoke tests on a clean Windows machine.
