# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## P0 - Reconcile and close the remaining Character Story authoring tail

Complete this sweep in dependency order. Do not infer review decisions from an
old TODO count, a GUI session or the presence of a WAV; every conclusion must
come from the current checksum-bound state, bundle and manifest authorities.
The reconciled baseline and exact remaining identities are recorded in
[`docs/character-story-authoring-census-2026-08-25.md`](docs/character-story-authoring-census-2026-08-25.md).

- [ ] Complete the real long-pause comparison pipeline: compare independent
      sentence segmentation with the center-only silence-compression candidate
      on an exact matched corpus, preserve equal text and speaker/control
      identity, publish raw and transformed WAV hashes plus the transform
      ledger, and expose the pair through a blind checksum-bound review bundle.
      Production selection remains unchanged until a human verdict exists.
      The exhaustive local scan found no eligible stored raw WAV; a fresh
      short-line capture was deliberately segmentation-ineligible and the
      known long-line capture ended typed limited. Keep this task blocked until
      a new exact long-line raw capture completes with one uniquely safe
      removable span; do not weaken the transform or reconstruct old evidence.

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

- [ ] Render and review the remaining typed non-verbal audio events. The exact
      parser, queue provenance and fail-closed speech filter are implemented and
      documented in
      [`authoring-audio-events.md`](docs/authoring-audio-events.md): canonical
      text/hash remain unchanged, while `Tsk!`, `*gasp*`, `*gurgle*` and unknown
      stage directions cannot be pronounced by ordinary MOSS or Pocket TTS.
      The exact 0.751-second Kanjira `Tsk!` game clip is now published as
      checksum-bound pending review
      `current-character-story-tsk-game-v1`, review ID
      `938ff6f824a1fe7ebb5e98b350d77dc16f7097f19604d94f17d59a1639364ac8`.
      Obtain one explicit accept/reject decision on it as a speaker-neutral
      tongue-click. Acceptance must then preserve the exact game
      event/bank/media/WAV hashes, explicitly disclaim Poacher I voice identity,
      and pass the final composition-ledger review documented below; rejection
      leaves the line unresolved rather than reading `Tsk` aloud.
      The bounded `/ǀ/` and `/ǃ/` Local 4B attempts are complete technical
      losses (`LIMITED` at the unchanged 3.0-second cap), and the ambiguous
      `common_npc05` media is excluded. When CUDA is available,
      evaluate official MOSS-SoundEffect v2 for isolated gasp/gurgle effects in
      its separate Python 3.12 environment. Require technical validation,
      perceptual approval and a checksum-bound speech/event/mix ledger; never
      claim that a generated effect inherited cloned speaker identity. Failed
      or unsupported effects stay unresolved or use an explicitly reviewed
      omission, never a silent production drop.
      The extractor's distinct ordered `story_audio_cues` are now strictly
      validated, preserved as producer-owned queue evidence and checksum-bound
      into the typed audio-event plan; legacy queues without that field retain
      their exact plan shape. Next, prefer an installed original event only
      after its semantic identity matches the requested effect;
      configured-unavailable event/bank IDs remain evidence, and a story SFX ID
      is never a voice reference. The regenerated patch 3.7 source index found
      adjacent cues for some `*pop*`, `*bang*`, `*buzzzzz*` and `*gasp*` lines,
      but every one is unavailable in the current install and the `gasp` cues
      are stream/water events rather than a proven human gasp. `Tsk!`,
      `*whimper*`, `*yelp*` and `*gurgle*` remain unbound.
- [ ] Resolve Poacher II failure
      `reverse1999:314606:62:e3f44f0529c8ced0` through the narrow immutable
      audit `current-character-story-poacher-ii-reference-audit-v1`. Compare
      its three blinded Centurion references on generated output before
      selecting an exact next reference hypothesis; raw-reference quality alone
      is insufficient. The exact three-arm render-only comparison
      `current-character-story-poacher-ii-reference-render-v1` is now immutable:
      candidates 01 and 02 completed, while candidate 03 ended typed limited and
      published no WAV. The checksum-bound listening command now accepts an
      explicit complete two-arm subset of the same comparison, retains the
      omitted-arm failure evidence and does not rerender either completed
      candidate. The one-trial blind session is published at
      `current-character-story-poacher-ii-reference-render-v1-listening`; hear
      both opaque sides and choose A, B or neither. A selected hypothesis still
      needs one bounded production render, technical validation, individual
      human review and exact outcome merge.
      `Neither` leaves the line failed for explicit live fallback; it does not
      authorize another MOSS seed or automatic Pocket transition.
- [ ] Resolve the five source-local Dobharchú failures before reusing this voice
      in another story. The four real cohorts are terminal: workspace
      `resume-395a5e5eec0327a3a793b66d-b3a3c14c9725777a` has 20 approvals and
      one rejection, and its exact accepted outcomes are already represented in
      the composed current workspace. Preserve that authority. Sentence repair
      still failed for
      `reverse1999:314605:102:1ab22c5fa4f30490`,
      `reverse1999:314605:95:ebc446c3c6e843bb` and
      `reverse1999:314608:8:7c5e047cb7785953`; exact reference comparison is
      still required for `reverse1999:314608:29:7be68e27f6d36933` and
      `reverse1999:314608:38:4988416dc161621c`. Do not retry any of those five
      until a new bounded, evidence-backed hypothesis and review gate exist;
      do not extend the portrait alias to unbound portrait `534705`.
- [ ] Resolve the exact 12-failure tail without another broad retry. Current
      post-reference reconciliation report
      `efa05a3fe7706a2983e170ce54c3b837bd451b8e597054ad59a700b02da084b4`
      has zero conflicts/merges and classifies only the four older Narrator
      lines `314608:58`, `314608:94`, `314606:43`, `314606:6` plus newly
      exhausted `314601:28`, `314601:47`, `314601:83`,
      `314601:96`, `314603:13`, `314603:14`, `314605:4` and `314608:86`
      as `new_hypothesis_required`. Preserve the completed evidence described in
      [`alternative-reference-comparison-2026-08-25.md`](docs/alternative-reference-comparison-2026-08-25.md)
      and do not repeat an exhausted seed/provider/repair. For each remaining
      failure, require a new bounded hypothesis, exact-ID successor, validated
      WAV, checksum-bound review and reconciled composed successor as separate
      transactions.

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
      raw evidence. The subsequent 2026-08-25 scan found zero eligible stored
      raw WAVs; a short exact capture was segmentation-ineligible and the known
      long-line capture ended typed limited. Do not spend another seed or raise
      the audio limit without a new bounded hypothesis and explicit
      authorization.
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

### P0 - Accept Narrator fallback role cues in real gameplay

- [ ] Enable the distinct `Narrator fallback roles only` setting for one real
      Character Story pass. Confirm that named fallback cues and the `Unknown`
      cue distinguish dialogue from true Narrator/Centurion lines without
      becoming repetitive, interrupting dialogue, or changing auto advance.
      Keep the mode disabled by default until this human listening gate passes.

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

- [ ] Make MOSS an identity-preferred but quality-routed primary rather than an
      automatically publishable default. Use the published checksum-bound
      human-labelled robustness corpus, but do not promote any current waveform,
      proportional-pause or Whisper signal into a reject gate: all three failed
      calibration, including reversed/noisy MOSS WER separation. Future cohort
      decisions now retain explicit independent defect reasons for pacing,
      repetition, truncation, pronunciation, timbre/artifact and speaker
      identity. Existing historical evidence has no such reasons and must remain
      honestly unclassified. Accumulate enough newly reviewed bad examples per
      provider/reason to reserve a
      held-out validation split, then compare a stronger local ASR or forced
      aligner before selecting thresholds. A production signal must demonstrate
      its false-positive and false-negative bounds on that untouched split.
      Until then route a non-passing MOSS line through the existing safe sentence
      repair when eligible, then one bounded provider-local retry, then a typed
      per-line XTTS or Pocket fallback; never weaken the strict silence gate,
      publish a truncated prefix or regenerate an already approved WAV. The
      direct listening verdict is that MOSS has the better voice identity, but
      its audible artifacts and unintended pauses still make unchecked output
      unacceptable. The complete v1/v2 corpus, timing and local-ASR results are
      durable in `docs/speech-robustness-corpus.md`.
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
