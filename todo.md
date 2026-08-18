# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Offline authoring and application responsibility split

### P0 - Maximize pregenerated coverage without losing speaker identity

Follow the evidence-backed order and invariants in
[`docs/pregeneration-coverage-plan.md`](docs/pregeneration-coverage-plan.md).

- [ ] Recover game-owned references for the current missing roles before using
      a fallback. Import and audit the installed same-speaker candidates for
      Aderyn, Dobharchú, Mrs. Owen, Hotelier, Poacher and Aderyn's Father,
      preserving exact bank/media hashes and keeping Aderyn's distinct
      portrait/bank variants separate until auditioned. This can potentially
      cover 214 of the current 237 missing-reference lines. Re-audit `Poacher
      I`, `Poacher II` and Glyndŵr without merging similarly named roles or
      treating configured-unavailable audio as installed.
- [ ] Add a versioned, explicit pregeneration missing-voice policy whose default
      is `block` and whose opt-in fallback can map an exact role list or all
      still-unresolved roles to Narrator. Preserve source speaker/character,
      record effective `Narrator -> Centurion` synthesis and all control hashes,
      and show source role and synthesis voice separately in review. Do not
      prepend speaker names to canonical story WAVs; evaluate a separate
      optional `announce speaker on change` accessibility route instead.
- [ ] Separate the selected Narrator fallback voice from force-live routing.
      With `prefer-generated`, approved Centurion WAVs must play first and
      Pocket/Centurion must handle only absent, pending, rejected, failed or
      invalid Narrator artifacts. A saved Centurion choice must not bypass all
      pregenerated Narrator tracks.
- [ ] Replace error-string-only failure handling with persisted typed cohorts
      and a report grouped by role, backend/model/profile, reference digest,
      attempt/seed, text length/punctuation, limit utilization and silence
      measurements. Reconcile the current 211 failures as 167
      audio-limit/missed-EOS and 44 speech-silence outcomes before retrying.
- [ ] Implement bounded cohort-specific prevention: evidence-based text
      normalization, safe sentence-boundary segmentation/concatenation for
      suitable long lines, limited seed diversification, reference comparison,
      and edge-silence handling. Keep the 20-second ceiling, never split inside
      a phrase, and do not reintroduce token-level duration control or mass
      retry a low-yield cohort.
- [ ] Add an immutable offline fallback-backend contract so exact cohorts that
      still fail MOSS can be pregenerated with Pocket TTS. Bind provider/model,
      provider-specific seed semantics, references and output hashes per item;
      carry forward unchanged terminal decisions without mutating the existing
      MOSS workspace. Apply the same WAV, silence and review gates to Pocket.
- [ ] Add an explicit terminal `live_fallback` route decision after source
      discovery and offline fallback generation are exhausted. Final hybrid
      publication must distinguish approved source/generated audio from a
      deliberate Pocket fallback; pending review and raw failed state remain
      nonterminal.
- [ ] Generate and review the 1,220 `no_audio` patch 3.7 lines with the approved
      primary model and fallback policy after references are ready. Preserve
      source-audio candidates, invalidate review on changed WAV hashes, review
      every technical flag plus a stratified clean sample, and expand review
      whenever that sample finds a substantive cohort defect.

## Live mode

### P0 - Repair failures observed in the latest Character Story run

- [ ] Re-run the chapter opening and verify that indexed incomplete prefixes
      are never spoken or auto-advanced. The 2026-08-16 run spoke only 43, 13,
      and 44 characters of the first three 94, 24, and 69-character lines;
      each fragment now resolves as one unique incomplete story prefix and must
      remain pending until the full line or a verified generated route is ready.
- [ ] Validate the split Narrator controls in a real Character Story run:
      approved pregenerated Centurion tracks take precedence, while Pocket uses
      Centurion only as the missing/failed live fallback. Retain Paper Heron as
      an ordinary character voice. The current approved-only manifest has 69
      entries; the 61 remaining clean Narrator WAVs need the risk-based review
      policy above rather than another unconditional listen-all gate.
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
      memory/persistent cache, first-chunk timing, realtime factor, and the
      missed-EOS safety cap. Underrun remains a real-device hardware acceptance
      gate because the render-only benchmark does not open an output device.
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

### P1 - Simplify the live speech boundary after the P0 replay passes

- [ ] Remove the deprecated concrete-backend `prepare()`/`play()` and mutable
      `last_*` compatibility facade in a major release after an external API
      usage audit and documented migration window. Internal playback and the
      benchmark already use typed call-bound results.

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
