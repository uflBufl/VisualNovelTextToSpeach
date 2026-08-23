# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Offline authoring and application responsibility split

### P0 - Complete the current Character Story in fail-closed order

Follow the checkpoint, dependencies and acceptance boundaries in
[`docs/current-character-story-completion.md`](docs/current-character-story-completion.md).

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
      Follow the immutable census and candidate contract in
      [`docs/dobharchu-repair-comparison.md`](docs/dobharchu-repair-comparison.md).
      Treat the evaluated composite only as a speaker-consistency candidate,
      never as a pause repair. Compare it with the 2.38-second reference only
      after both use the selected corrected-pause strategy documented below.
      Review the published bundle
      `current-character-story-dobharchu-natural-expansion-v1.json`: it contains
      exactly 24 pending WAVs in four cohorts, comprising 17 direct natural
      outputs and seven bounded sentence repairs. Every item is currently a
      mandatory sample because it is technical-attention evidence or the
      deterministic clean sample for its exact portrait/reference cohort. The
      current checksum-bound progress is `2/4` terminal cohorts, with 17 exact
      samples/items remaining in the two unresolved reference cohorts according
      to the 2026-08-23 read-only status reconciliation. Apply only explicit
      source-local cohort decisions, then merge terminal reviewed
      repair outcomes into successor
      `resume-395a5e5eec0327a3a793b66d-b3a3c14c9725777a`; preserve its four
      existing approvals, the 15 primary approvals, the two accepted portrait
      variants and all unrelated state. Do not extend the portrait alias to
      unbound portrait `534705` or weaken the global silence gate. Five failures
      remain outside this review: sentence repair still failed for
      `reverse1999:314605:102:1ab22c5fa4f30490`,
      `reverse1999:314605:95:ebc446c3c6e843bb` and
      `reverse1999:314608:8:7c5e047cb7785953`; exact reference comparison is
      still required for `reverse1999:314608:29:7be68e27f6d36933` and
      `reverse1999:314608:38:4988416dc161621c`. Do not retry any of those five
      until a new bounded, evidence-backed hypothesis and review gate exist.
- [ ] Finish the exact 22-failure tail in merged workspace
      `resume-395a5e5eec0327a3a793b66d-1c6ff408bbe999e0` without another broad
      retry. Its current read-only repair projection is 11 safe
      sentence-boundary segmentations, seven exhausted-primary Pocket
      fallbacks, one final provider-local bounded MOSS seed and three Narrator
      reference/silence investigations. First publish and execute one
      config-addressed MOSS successor containing only the 11 sentence IDs and
      one bounded-seed ID, then recompute the plan from its authoritative state.
      Publish a separate Pocket successor only for items that the recomputed
      provider-local plan marks `offline_fallback_backend`; never render the
      three reference-comparison items without a new exact hypothesis. Preserve
      all 190 approvals, every unrelated state/WAV hash and the 190-entry
      approved-only manifest. Review new WAVs through a checksum-bound bundle,
      merge only terminal decisions, and leave generation, review and final-pack
      publication as separate transactions.
      The first exact 12-item successor produced no WAV. Its corrected
      read-only projection selected six exact different-backend fallbacks, one
      final bounded MOSS seed, four reference comparisons and one inline-pause
      comparison; a failed sentence segmentation cannot be planned or executed
      twice. The six Pocket fallbacks are now generated and await exact review.
      The final bounded MOSS seed failed `missed_eos` without a WAV; its
      corrected one-ID Pocket successor generated successfully. Review all
      seven final Pocket WAVs through checksum-bound bundle
      `current-character-story-final-pocket-fallbacks-v1.json`: it contains
      three required cohorts and seven mandatory samples, including five with
      technical silence/pause or pace flags. Apply only explicit source-local
      cohort decisions, then merge terminal outcomes into a new successor.
      Keep the four reference-comparison items unrendered and treat the
      inline-pause item as a separate one-ID comparison rather than part of the
      fallback batch.
- [ ] Recalibrate review-only silence attention after the seven-WAV Pocket
      bundle is terminal. At least one item currently labelled `notable
      silence` was explicitly judged by the listener to contain no abnormal
      pause, so treat that label as advisory during this review rather than as
      rejection evidence. Preserve the strict publication safety checks; use
      the checksum-bound accepted/rejected examples to measure false positives,
      adjust only the review-risk thresholds or feature definition, and make
      the UI say that technical attention is a non-blocking diagnostic. Add
      regressions proving a natural accepted pause is not presented as a likely
      defect while genuinely long internal silence remains visible.

### P0 - Make long-pause repair automatic and provenance-safe

Follow the measured Dobharchú attribution in
[`docs/dobharchu-repair-comparison.md`](docs/dobharchu-repair-comparison.md).
The composite prompt is not a pacing repair: exact composite/single-reference
controls both produced roughly three-second silence at sentence boundaries,
while one-sentence controls remained below 0.25 seconds.

- [ ] Publish and blind-review a small real checksum-bound corpus comparing
      independent sentence segmentation with the implemented center-only
      silence-compression candidate. The comparison primitive already rejects
      ambiguous text, multiple notable spans and removable centers containing
      low-level speech, breaths or music; it retains 600 ms of measured boundary
      context and cannot mutate generation state. Keep it out of production
      unless blind review shows equal words and speaker identity with better
      cadence than segmentation. Never cut generation at the first long silence
      and publish the prefix: valid words may follow it. A future streaming
      cutoff may only fail the current already-segmented unit for bounded
      retry/fallback. Use the explicit one-queue-ID, one-attempt evidence sink
      only for a newly justified retry; current old failures correctly deleted
      non-publishable partial WAVs and cannot be reconstructed as the same
      evidence. The first authorized capture attempt for
      `reverse1999:314605:102:1ab22c5fa4f30490` reached the typed audio limit
      before silence validation, so it published neither production audio nor
      raw evidence. Do not spend another seed or raise the audio limit without
      a new bounded hypothesis and explicit authorization.
- [ ] Apply the selected repair policy to pending/failed Dobharchú items in a
      new config-addressed workspace. Review every transformed WAV and a
      deterministic clean control sample. Acceptance requires no internal pause
      above 1.2 seconds, no truncation/repetition/word change, natural perceived
      boundaries, unchanged speaker identity, exact state/WAV/control hashes,
      approved-only manifest derivation, and no mutation of source workspaces.
      Treat inline markers only as a bounded candidate after the mixed real
      sample: all five final outputs eventually passed the technical gate, but
      only three were approved; one was rejected for perceptually large pauses
      and one remains unapproved after sounding slightly unnatural. Two lines
      needed their final allowed seed-2 attempt. Never auto-approve marker
      outputs or replace safe sentence segmentation with marker insertion.
- [ ] Reuse the same classifier and safe segmentation in live mode only after
      the offline gate passes. Pre-segment eligible multi-sentence text before
      playback so the UI never waits through a known multi-second silent run;
      preserve cancellation and stale-generation guards between segments. Use a
      previously approved fallback for unsafe short clauses rather than doing
      audible multi-seed retries during gameplay.
- [ ] Finish the workbench repair review after a real comparison bundle exists:
      offer immediate replay of both checksum-bound versions without changing
      selection, bind approval to the repaired WAV and transform ledger, and
      retain raw failure evidence on rejection. The workbench already labels
      eligible failures as `Long sentence-boundary pause` and shows measured
      raw/repaired pause durations without making failed audio reviewable.

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
      an ordinary character voice. The current approved-only manifest has 77
      entries; the 128 current-provenance pending WAVs need the risk-based
      three-cohort review plan rather than another unconditional listen-all gate.
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
      line that requires live synthesis. Use `vntts-capture-live-replay` so
      accepted cropped frames and their SHA-256 ledger are retained, then review
      every inferred boundary in `capture-report.json` before replaying
      `corpus.json`; the tool deliberately does not turn capture-time OCR into a
      declared-observation fixture.
- [ ] Pass a 20-line real-game acceptance run with no stale or duplicate speech,
      no skipped dialogue, no early advances over original audio, and automatic
      progression of every tested dialogue line.

### P0 - Establish a MOSS quality and latency gate

- [ ] When a suitable CUDA host becomes available, run MOSS Delay 8B on the
      installed checksum-bound 46-line corpus (22 unresolved MOSS failures, 12
      MOSS-to-Pocket recoveries and 12 MOSS controls). No CUDA host is currently
      available, so this remains an optional future candidate and does not
      block the current model decision. Retain the transactional Delay report
      and compare its per-group outcomes, exact WAV hashes, timing/RTF,
      duration, silence/quality and hardware/model provenance against MOSS Local
      4B. Do not rerun XTTS or the completed Local4B/XTTS blind task merely to
      claim a three-way comparison, and do not attempt the 8B corpus on the
      current CPU-only path. The authoritative local phase is complete at
      `offline-local-4b-vs-xtts-20260823-v3`: exact manifest resolution maps
      Narrator to Centurion, both reports share all 46 ordered identities and
      bind the same 12 copied voice controls with canonical SHA-256
      `a41a40b7457e9d5c8e50ef0f646991c3107e4c43380a578ebe3f707705e85192`.
      MOSS produced 44 complete/43 silence-gate-passing results and XTTS
      produced 46 complete/46 gate-passing results; XTTS has unsupported shared
      seeding, so retain this as one stochastic run. The earlier `v1` run is
      invalid because of speaker fallback, while `v2` established exact speaker
      identity but did not snapshot reference bytes. The completed ten-trial
      blind task ranked MOSS first: 7 MOSS wins, 1 XTTS win, 1 no-preference and
      1 neither-acceptable. Keep MOSS Local 4B as the offline authoring default;
      do not regenerate approved WAVs. If Delay 8B is tested later, compare it
      only against MOSS in a new bounded blind follow-up.
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
