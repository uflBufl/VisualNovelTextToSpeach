# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Offline authoring and application responsibility split

### P0 - Complete the current Character Story in fail-closed order

Follow the checkpoint, dependencies and acceptance boundaries in
[`docs/current-character-story-completion.md`](docs/current-character-story-completion.md).

- [ ] Review both published checksum-bound specialist bundles with
      `uv run vntts-review-bundle BUNDLE.json`. Version 2 has 81 required
      samples for the original 99 pending WAVs; the selected follow-up bundle
      has 38 required samples for only the 45 newly generated WAVs (one final
      MOSS sentence repair and 44 Pocket fallbacks), with inherited pending WAVs
      excluded. Every technical-attention WAV remains mandatory; clean
      short/medium/long items are sampled deterministically. Use sample-level
      bad markers and `Need another sample` instead of accepting a doubtful
      cohort. These reviews intentionally exclude six terminal specialist
      silence failures, 29 reference-comparison items and one new pending WAV
      in the primary workspace. After terminal bundle decisions, inspect the
      exact source-local evidence before merging outcomes.
- [ ] Review the published version-2 operator task for all 29
      reference-comparison failures at
      `authoring/review-bundles/current-character-story-reference-audit-v2`
      (audit ID
      `52fc3aa6e545109b79bbce7f1842ae5f1428c2f467521f362b3b09832f53223e`).
      The original version-1 audit
      `7e18a82836e6e79a6f4a50b1e11d04f2fd87cfbae00ccabdf480f3fd3b1b3d8a`
      is rejected before operator use because its private candidate mapping was
      not part of the public canonical identity. Version 2 covers the same four
      exact control
      groups: three blinded Centurion candidates for 23 Narrator cases, three
      blinded Rhiannon candidates for four cases, and one exact candidate each
      for Aderyn and Poacher. Run `uv run vntts-reference-audit AUDIT_DIRECTORY`:
      it plays copied checksum-bound bytes without exposing source names,
      offers every opaque candidate plus `Neither candidate is acceptable`, and
      saves in a background worker without disabling playback. After all four
      decisions, inspect the canonical decision-set ID and private mapping;
      these decisions are reference evidence only. Publish a new explicit
      selected reference/control binding before any regeneration.
- [ ] After the repair WAVs are reviewed, run the checksum-bound
      `merge-workspace-outcomes` command to create one successor containing the
      77 primary approvals plus only exact approved/rejected sentence and
      Pocket repair outcomes. Inspect its source-state/item/WAV ledger and
      approved-only manifest before using the successor for generation.
- [ ] Generate newly unblocked reference lines under immutable controls as
      references become available, then apply the same risk-based cohort review
      and exact outcome merge. The previous ten ready lines are complete: one
      produced a pending-review WAV and nine now have typed repair evidence.
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
      deterministic clean sample for its exact portrait/reference cohort. Apply
      only explicit source-local cohort decisions, then merge terminal reviewed
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

### P0 - Make long-pause repair automatic and provenance-safe

Follow the measured Dobharchú attribution in
[`docs/dobharchu-repair-comparison.md`](docs/dobharchu-repair-comparison.md).
The composite prompt is not a pacing repair: exact composite/single-reference
controls both produced roughly three-second silence at sentence boundaries,
while one-sentence controls remained below 0.25 seconds.

- [ ] Add a bounded quality-first path for lines that cannot be segmented safely,
      including two-word clauses such as `What happened? You're hurt.` Try at
      most three deterministic seeds and accept only a `complete` render that
      also passes the unchanged silence gate. If all fail, publish no MOSS WAV
      and route the exact item to its configured typed offline fallback; when no
      identity-compatible fallback exists, leave it explicit manual-review work.
      Do not globally rewrite punctuation, force token-level duration or raise
      the 1.2-second internal-silence limit. Do not spend the acceptance budget
      on a broad temperature/top-k/top-p/repetition grid: external reports show
      the failure is prompt-sensitive and those sweeps are not a reliable cure.
- [ ] Compare segmentation against a conservative silence-compression candidate
      on a small checksum-bound corpus before deciding whether waveform repair
      is worth supporting. Compression may remove only the center of a single
      punctuation-aligned silent span while retaining measured boundary context;
      it must fail closed for ambiguous spans, low-level speech, breaths, music
      or multiple unmatched boundaries. Keep it out of production unless blind
      review shows equal content/voice and better cadence than re-rendered
      segments. Never cut generation at the first long silence and publish the
      prefix: valid words may follow it. A future streaming cutoff may only fail
      the current already-segmented unit for bounded retry/fallback.
- [ ] Apply the selected repair policy to pending/failed Dobharchú items in a
      new config-addressed workspace. Review every transformed WAV and a
      deterministic clean control sample. Acceptance requires no internal pause
      above 1.2 seconds, no truncation/repetition/word change, natural perceived
      boundaries, unchanged speaker identity, exact state/WAV/control hashes,
      approved-only manifest derivation, and no mutation of source workspaces.
      Treat inline markers only as a bounded candidate after the mixed real
      sample: three of five attempts passed the technical gate, two failed
      closed, and one passing WAV still sounded slightly unnatural. Never
      auto-approve marker outputs or replace safe sentence segmentation with
      marker insertion.
- [ ] Reuse the same classifier and safe segmentation in live mode only after
      the offline gate passes. Pre-segment eligible multi-sentence text before
      playback so the UI never waits through a known multi-second silent run;
      preserve cancellation and stale-generation guards between segments. Use a
      previously approved fallback for unsafe short clauses rather than doing
      audible multi-seed retries during gameplay.
- [ ] Expose the repair in the workbench as `Long sentence-boundary pause`, show
      measured raw/repaired pause durations and offer immediate replay of both
      versions without changing selection. Approval must bind the repaired WAV
      and transform ledger; rejection must retain the raw failure evidence.

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
