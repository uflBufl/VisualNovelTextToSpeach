# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Authoring workbench UX redesign

### P0 - Review the complete workbench information architecture and workflow

- [ ] Audit the end-to-end tasks separately: workspace loading, collection
      selection, generation, individual review, cohort review, reference
      audition and diagnostics. Redesign the default screen around the active
      task instead of showing every subsystem at once; move secondary generation
      controls, voice-reference browsing and technical logs into clearly named
      tabs or collapsible detail panels.
- [ ] Make review the primary workspace: keep the audio controls, selected line,
      decision controls and pending navigation together; show Speaker, effective
      synthesis voice (for example `Narrator -> Centurion`), status, attempts
      and quality warnings without requiring horizontal scanning. Filters must
      make Narrator-only and character-only work obvious and must never silently
      mix unrelated voices into a focused cohort task.
- [ ] Add drill-down counts behind the compact Review/Coverage/Selection
      summary. Finish applying the established source-speaker/effective-voice
      terminology to pending review, technical attention, missing reference and
      failed-generation details.
- [ ] Define predictable sizing and persistence: useful minimum window size,
      readable column defaults, resizable panes, remembered filters and selected
      task, no clipped controls and no large empty diagnostics panel by default.
      Review the interface visually at compact laptop and large desktop sizes.

### P1 - Prove responsiveness, accessibility and real-workspace usability

- [ ] Keep all integrity projection, WAV preparation, review publication and
      manifest rebuild work off the Qt thread. Replay, Accept and Reject should
      acknowledge immediately, keep navigation responsive and show bounded
      progress; add event-loop heartbeat tests for cold load, replay and both
      decision paths on a 592-item fixture.
- [ ] Specify and test focus order, accessible names, status announcements,
      contrast-independent state, screen-reader labels and keyboard-only review.
      Media and decision actions must remain reachable after table selection,
      playback completion, errors and asynchronous reloads.
- [ ] Add deterministic Qt tests for repeat playback, previous/next stability,
      disabled-action reasons, media failure recovery, stale authority, rapid
      sequential actions and close-during-operation behavior. Keep exact WAV,
      queue, state and lease checks fail-closed while making those failures
      understandable and recoverable.
- [ ] Run a fresh manual acceptance on the real 592-line Character Story
      workspace. Measure open, replay, Accept and Reject responsiveness; verify
      both current Centurion cohorts can be reviewed without unrelated Narrator
      rows or control movement; capture screenshots and move the accepted UX
      contract and measurements to `docs/` before removing this section.

## Offline authoring and application responsibility split

### P0 - Complete the current Character Story in fail-closed order

Follow the checkpoint, dependencies and acceptance boundaries in
[`docs/current-character-story-completion.md`](docs/current-character-story-completion.md).

- [ ] Regenerate the remaining legacy pending-review WAVs (150 at the
      documented checkpoint) as bounded exact-ID batches under current
      immutable controls. Do not sample-approve them, assign the current profile
      to old WAVs, or mutate imported history.
- [ ] Repair only the 18 current-provenance failures with exact-ID bounded
      plans, then generate the ten currently ready lines and newly unblocked
      reference lines under immutable controls.
- [ ] After the detailed reference and quality tasks below are complete,
      rebuild the approved-only manifest, require terminal coverage or an
      explicit supported fallback for all 592 queue items, and run the real
      Character Story routing and auto-advance acceptance.

### P0 - Maximize pregenerated coverage without losing speaker identity

Follow the evidence-backed order and invariants in
[`docs/pregeneration-coverage-plan.md`](docs/pregeneration-coverage-plan.md).

- [ ] Acquire a new intelligible Mrs. Owen reference and a Hotelier reference
      that passes the minimum-duration gate, then repeat only their exact
      cluster evaluation and quality review. The expanded review is complete
      `7/7`: six accepted variants bind 73 exact queue IDs, all 15 terminal
      Rhiannon decisions were carried into the new config-addressed workspace,
      and preflight reduced missing references from 184 to 164 lines. Mrs.
      Owen remains `needs_sample`; Hotelier has no technical pass. Keep 41
      lower-priority unreviewed candidates pending rather than promoting them
      automatically. Preserve Aderyn as source identity, keep adult and child
      portrait groups separate, retain crying-only rejects, and publish a new
      immutable quality decision/binding that excludes or replaces the current
      child variant: real-story synthesis sounded bad even though its source
      clip passed the earlier fixed evaluation. Do not mutate the v3 review or
      v4 binding. Do not merge `Poacher I`, `Poacher II` or Glyndŵr or treat
      configured-unavailable audio as installed.
- [ ] Finish review of the remaining technically valid source-bound Character
      Story WAVs using
      every technical-attention item plus a stratified clean sample from each
      of the six exact reference variants. Expand a variant's review if the
      sample finds a substantive voice, pronunciation, pacing or contamination
      defect. The initial Aderyn pass approved two natural adult lines, rejected
      four adult lines with pauses/slow pacing and rejected one child line.
      Reject only listened artifacts; do not promote the remaining child output
      or apply adult decisions across unrelated portrait variants. Exclude the
      seven remaining Dobharchú outputs from this listen-through: the expanded
      real-story sample already found a cohort-level pacing/pause defect, so
      leave those exact items pending until the repair comparison below.
- [ ] Repair the Dobharchú synthesis cohort before reusing it in another story.
      Preserve the 11 exact approved WAVs; do not promote either whole portrait
      variant. The current state has 19 listened rejects, seven pending and 11
      failed Dobharchú items. Build a successor immutable comparison over those
      37 unresolved items using bounded alternative reference/profile/backend
      candidates, with token-level duration control still disabled. Add a
      conservative slow-pace flag and a pause-sensitive gate that catches the
      reviewer-observed inter-phrase pauses missed by the current zero-silence
      metric. Review a deterministic short/medium/long sample per exact portrait
      variant and expand only when that sample finds another substantive defect.
- [ ] Add an explicit reusable voice-quality gate across later stories. The
      checksum-bound workbench cohort flow now samples technical and clean WAVs,
      counts only completed exact-byte playback, records immutable
      accept/reject/expand evidence and atomically projects terminal per-item
      decisions. Build a separate reusable gate keyed by exact voice variant,
      ordered reference hashes, backend/model/profile and synthesis policy, then
      sample each newly generated story cohort before reusing it. Never silently
      carry approval across changed controls or an unreviewed age/portrait
      variant.
- [ ] Repair the remaining 18 source-bound failures without broad retries:
      run the 12 exact sentence-boundary candidates with checksum-bound segment
      provenance, move the four three-attempt missed-EOS items to the configured
      offline fallback backend, and manually audition alternatives for the two
      exact `reference_comparison` silence failures before `select-reference`.
      Objective metrics cannot decide speaker identity, background
      contamination or pronunciation. Keep the 20-second ceiling.
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
