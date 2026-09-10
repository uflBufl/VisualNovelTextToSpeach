# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements in agent memory and completed-work history in Git, not here.

## P0 - Qualify Windows narrator preview latency

- [ ] Finish the approved Local GPU experiment without changing production
      defaults prematurely. Preserve the accepted three exact candidate WAVs;
      do not request that listening or the unchanged ABBA benchmark again.
  - Trace CPU waveform decode/reference encode for avoidable repeated work and
    supported selective acceleration; choose the smallest measured candidate.
    Keep codec working buffers and GPU memory budget explicit.
    Compare approved Local GPU + four CPU workers against Local GPU + eight
    CPU workers, with the persistent pool OFF in both to isolate thread count.
    Reuse the existing graph checks, same sampling/reference and short launcher;
    require real warm codec/total improvement without output or memory regression.
  - Build/check any native change separately, retain the approved Local GPU
    build as baseline, and prepare a short target-PC qualification command.
    Stop at real Windows GPU/quality gates that cannot run on this Mac.
    Locate the eight-worker stall inside its first synthetic graph cycle (still
    incomplete after 120 seconds); retain exact outputs and ownership cycles,
    do not extend the timeout again or distribute this unqualified candidate.
- [ ] Verify successful preview replay on Windows with the qualified native profile;
      reuse the saved WAV without a new generation. Request another support export
      only for an unresolved problem, not to repeat already measured retry/device
      behavior. Acoustic quality remains a separate check from technical gates.
- [ ] Reduce native CPU audio-frame generation and waveform decoding time.
  - Reduce first-use reference encoding cost without duplicating the existing
    server-lifetime code cache. Consider encoder acceleration or explicit
    preparation only after separating first-use work from repeated synthesis.
  - Evaluate selective native GPU placement, starting with tensor sizes and
    temporary buffers for the Local frame decoder versus waveform codec.
    Do not enable the whole auxiliary sidecar on the 8 GB target by default.
    Gate any prototype on supported operations, memory headroom, unchanged
    output quality, cancellation and measured end-to-end improvement.
    Qualify remaining cancellation, idle and real-model lifetime behavior before
    adoption; target-PC speed/memory measurements and the three sample listening
    decisions are complete, not blanket approval for future generations.
  - Assess an isolated PyTorch Local 1.5 comparison (not the existing Delay 8B
    adapter): quantify model/tokenizer memory first, then qualify quantization
    and Turing-compatible precision before any target GPU render. Reuse the
    reference and comparison reporting infrastructure; no automatic promotion.
  - Select the implementation only after comparing cold/warm time, RAM/VRAM,
    audio correctness and Windows installation complexity. Keep production
    placement and sampling unchanged during experiments.
  - Use support-export `reference_prepare_s`, `http_round_trip_s` and
    `response_pcm_decode_s` to distinguish client work from native execution.
    Add a reference-conversion cache only if Windows measurements justify it;
    server and registered reference-code reuse already exist.
  - For CPU thread tuning, change and qualify the native runtime first: pinned
    openmoss v0.3.0 exposes no thread CLI option. Python/PyTorch thread or CUDA
    settings do not tune this C++/Vulkan runtime. Preserve server-lifetime
    reference-code reuse and intentional cancellation/error teardown.
  - Before adopting the CPU-pool candidate, qualify safe shutdown/cancellation,
    idle CPU/RSS and CPU-only behavior. Reuse the completed ABBA measurements;
    do not repeat the same comparison unless the implementation changes.
  - Qualify native backbone ownership with repeated real-model load/unload,
    failed context creation and request shutdown: no leaks or double frees.
    The cleanup is shared by both diagnostic variants; compilation and no-model
    pool checks do not exercise live libllama context/model destruction.
  - Evaluate codec-only acceleration separately from auxiliary-decoder placement;
    require a supported native build and memory/quality measurements before
    enabling it by default. Native reference-cache hit and EOS stop-reason
    reporting require upstream instrumentation; missing logs and frame counts
    alone cannot distinguish cache reuse or natural EOS exactly at the limit.
  - Keep the CPU auxiliary default until measured RAM/VRAM and output quality
    justify a different placement. Do not place the entire Q8 sidecar on an 8 GB
    GPU blindly. Any optimization needs comparable fresh cold/warm Windows runs,
    memory measurements and audio-quality verification before claiming a speedup.

## P0 - Prefer spoken playable-character narrator references

- [ ] Measure first-play latency with actual Windows game audio after selection
      prefetch; synthetic Mac decoder timing is not a Windows performance gate.

## P1 - Diagnose intermittent offscreen Qt test stalls

- [ ] Capture thread stacks when the macOS `qt-app` shard stalls in
      `test_settings_are_scrollable_and_grouped_into_visual_regions` (180-second
      timeout). Ten fresh-process repeats passed without reproducing the stall.
      The runner now dumps thread stacks before its unchanged timeout; inspect
      the next failing transcript for leaked event/modal/worker state and fix
      only the demonstrated cause, then repeat the shard and runner tests.

## P2 - Automate the fresh-install player journey (deferred until UI redesign)

Scope: the GUI entry point is `uv run vntts-app` (`vntts.app.main`), not
the console hotkey reader `uv run vntts`. Prove that a new player can import
installed content, choose a game narrator, prepare one story, activate its audio,
restart and read the saved recordings without configuring the same things again.
Preserve this plan, but do not strengthen these tests now. After the redesigned
journey is accepted, adapt the steps below to it (especially onboarding and
automatic activation) before implementing; do not freeze the old interface.

- [ ] Build on the existing tests, without adding a second test framework.
  - Extend `tests/test_self_service_pregeneration.py` and reuse its synthetic
    renderer, real generation/recovery/acceptance/publication services and Qt
    helpers; reuse narrator-picker and generated-audio routing fixtures.
  - Keep existing component coverage. Rename the current "process restart"
    test to describe same-process dialog reopening; add a genuine process test
    instead of treating newly constructed widgets as a fresh application.
  - Use valid synthetic WAVs for references as well as generated output, not
    the placeholder bytes used by metadata-only fixtures.
- [ ] Establish a genuinely empty, isolated application environment first.
  - Use temporary settings/config/data/cache/import roots, the existing
    `VNTTS_SETTINGS_FILE` override and test-side directory injection before
    consumers import directory helpers. Clear inherited VNTTS/extractor/model
    overrides and disable default reference/artifact discovery outside the fixture.
  - Keep production discovery of the fixture's imported catalog enabled. Check
    that settings, profiles, voice decisions, narrator bindings, jobs and packs
    stay inside the sandbox; a clean settings file alone is not isolation.
  - Forbid network/model downloads, native audio, real OCR/capture/input dispatch,
    tray/login registration and access to the user's installed game. Supply
    deterministic external results; do not mock persistence or audio routing.
- [ ] Exercise the real GUI handoffs in the first child process.
  - Start the application with no saved setup. Assert that only onboarding is
    visible and speech-dependent controls are gated while work is pending.
  - Drive actual wizard buttons and diagnostics/test callbacks with synthetic
    game/capture/OCR/speech results; finish through the normal save path, not by
    setting `onboarding_completed` or accepting a mocked dialog.
  - Open preparation from the dashboard. Start with no discovered content;
    exercise the import button and real importer result parsing/catalog discovery,
    replacing only the extractor process at its external boundary.
  - Import at least two stories, select only one, open the real game-narrator
    picker, load a Centurion reference, preview and save it. Keep at least one
    independently voiced character to catch accidental narrator-for-everyone
    routing. Use reference-conditioned MOSS settings with synthetic synthesis;
    retain the existing Pocket built-in-voice coverage separately.
  - Confirm that dashboard, picker, preparation summary and final voice routes
    agree on narrator, engine and model. Generate through real queue/publication
    logic, then click `Use prepared audio` through the real activation handler;
    do not replace the preparation dialog or its accepted result with a mock.
  - Assert progress is visible, incompatible actions are disabled, final counts
    match saved artifacts, and activation returns to a clear `Start reading`
    action without starting playback automatically.
- [ ] Verify persistence and actual recording use in a second child process.
  - Exit the first process cleanly and launch another against the same temporary
    user directory, without passing in its settings object or store instances.
  - Load settings and the pack through production paths. Assert onboarding does
    not repeat; both imported stories remain listed, the selected story retains
    its coverage and the unselected story remains available but unprepared.
  - Verify the chosen narrator's source character and reference checksum, not
    only the assignment string: pack activation can rebind it as
    `character:narrator`. Preserve engine/model and the other character's route.
  - Feed a known prepared dialogue through the production controller/resolver
    and generated-audio backend into a recording-only audio sink. Assert the
    exact line/WAV identity, expected PCM/sample rate and generated route; fail
    on any synthesis, voice-decision prompt, reimport or regeneration for it.
- [ ] Add the journey to the existing macOS/Windows/Linux unit-test CI jobs.
  - Use Qt condition waits with deadlines, not arbitrary sleeps or tight timing
    assertions. Run the journey repeatedly with fresh roots to expose races.
  - On failure, retain phase, child-process output, UI state/screenshot and the
    synthetic workspace as CI artifacts. On success, leave no child workers.
  - Completion gate: both-process journey passes on the existing OS matrix;
    targeted component tests, formatting and lint pass. No new dependencies or
    production test-mode flags.

Not covered by this deterministic test: real voice quality, actual game import
format compatibility, model/decoder installation, OS permissions, native focus
or audio-device behavior. Keep the real fresh-install qualification below.
Interruption/recovery expansion and packaged-executable smoke coverage remain
separate follow-up work, not prerequisites for this first journey.

## P1 - Run MOSS Local v1.5 on Windows

- [ ] Qualify real Windows CPU/8 GB GPU rendering with
      normal `uv run vntts-app` first-launch setup and, for GPU tuning,
      `scripts/run-moss-windows.ps1`: record memory use, latency and blind accent
      fidelity against MLX with the same character references. Verify native
      DLL loading, cancellation/reload and shutdown on the target machine.

## P0 - Choose a game narrator on a fresh install

- [ ] Qualify the guided game-narrator picker on a clean macOS/Windows install:
      auto-detect the game (and try folder fallback), listen to one original and
      generated reference with the intended engine/account access, save, restart,
      then confirm both live fallback and offline preparation retain the choice.

## P1 - Qualify desktop UX on real platforms after the player redesign

Use [`docs/ui-ux-review-plan.md`](docs/ui-ux-review-plan.md) and
[`docs/ui-ux-control-audit.md`](docs/ui-ux-control-audit.md) as historical
control-level evidence, not as approval of the present player journey. Qualify
the redesigned main interfaces after the P0 design/implementation work above.

- [ ] Qualify the resulting journeys on real macOS and Windows with
      native dialogs/tray behavior, fullscreen and multi-monitor use,
      VoiceOver/Windows Narrator, physical 100%/150%/200% scaling, and
      representative novice, returning-player and specialist tasks.

## P0 - Make pregeneration self-service

Follow
[`docs/self-service-pregeneration.md`](docs/self-service-pregeneration.md).
Pregeneration is an ordinary-user workflow: a player selects installed story
content, confirms only a small number of ambiguous character voices, and lets
VNTTS build and activate a local game pack without exposing authoring concepts.

- [ ] Calibrate automatic preview and bulk-quality routing without expanding
      mandatory review. Collect enough independently reason-labelled bad outputs
      for pacing, repetition, truncation, pronunciation, artifacts and speaker
      identity to reserve fit and held-out partitions; compare a stronger local
      ASR or forced aligner, and promote a rejection rule only after measuring
      false positives and false negatives. Follow the corpus-v3 evidence in
      [`docs/speech-robustness-corpus.md`](docs/speech-robustness-corpus.md);
      until then keep existing signals diagnostic-only and use safe sentence
      repair, one bounded provider-local retry, then typed XTTS/Pocket fallback.

## P1 - Improve the self-service generation engine

Follow
[`docs/pregeneration-coverage-plan.md`](docs/pregeneration-coverage-plan.md)
and keep original audio, approved generated audio, explicit live fallback and
intentional omission as distinct terminal authorities.

- [ ] Complete real speaker-identity threshold validation with the diagnostic
      harness in
      [`docs/speaker-identity-diagnostics.md`](docs/speaker-identity-diagnostics.md).
      Label independently reviewed fit and held-out `same-speaker`,
      `different-speaker` and `same-character/different-age` pairs from the
      installed checksum-bound inventory. Publish a downstream threshold only
      if the fit groups separate and held-out evaluation preserves every known
      age/identity boundary; until then keep all current variants separate.

## P1 - Upgrade and unify Python runtimes

Use Python 3.14 for the application and every speech runtime that can pass its
real model smoke test. Select the newest compatible stable release rather than
blindly upgrading an atomic model stack beyond its upstream-supported versions.
Every retained upper bound or exact pin must name the observed incompatibility
in the same commit.

- [ ] Qualify the Python 3.14 MOSS Delay candidate on Windows CUDA. It retains
      an upstream-compatible Transformers 5.0 pin while trialling the current
      Torch/TorchAudio 2.11, TorchCodec 0.16 and CUDA 13.0 atomic stack. Install
      a supported full-shared FFmpeg 4-8 build, run the CUDA probe and require
      one real checksum-bound reference-conditioned render before accepting it.
      Trial the newest Transformers only after that baseline passes, and retain
      5.0 if output, VRAM, loading or generation compatibility regresses.
  - Acceptance: CUDA import and one checksum-bound render pass on Windows, with
    a CPU-only host producing a clear unsupported-backend result rather than
    loading the 8B model.
- [ ] Qualify the Python 3.14 MOSS SoundEffect v2 candidate on Linux CUDA. The
      lock now overrides upstream's wheel-less NumPy 1.26.4 pin with NumPy 2 and
      keeps the Torch 2.9/CUDA 12.8 family aligned while trialling TorchCodec
      0.9.1. Require import plus one fixed-seed effect render with finite PCM and
      clean worker shutdown. If `descript-audiotools` or model code fails under
      NumPy 2, record the exact failure and retain Python 3.12 for this isolated
      runtime without blocking the other upgrades.
- [ ] Extend automatic runtime preparation after the CUDA upgrades are proven.
  - Qualify the app-managed first-install journey on clean macOS and Windows:
    absent source environment, interrupted download, retry, restart, then one
    real render. Keep model/license consent separate from dependency setup.
  - Gate downloadable runtime installation in portable releases on published,
    integrity-verified runtime artifacts; do not invent download URLs or ask
    portable users to install uv.
  - Use driver detection to select a qualified CUDA stack only after the real
    render gates above pass. Missing/unknown NVIDIA-driver evidence must never
    promote a CUDA candidate; users must not select Python or CUDA wheels.
  - Add actual CUDA dependency/import and model-render smoke tests on a
    self-hosted Windows/Linux runner once one is available. Keep the hosted
    CPU-only checks dependency-only; do not download or load model weights there.

## P1 - Complete distributable release packages

- [ ] Make release packages able to run the backend they recommend by default.
  - Follow [`docs/release-speech-runtime.md`](docs/release-speech-runtime.md).
    Retain repository/revision/checksum evidence for every downloaded model and
    voice. Do not bundle gated weights or unclear/non-commercial voices without
    a release-owner approval covering those exact files.
  - Complete the Developer ID signed/notarized macOS build plus the signed
    Windows portable build before removing this item. Acceptance requires
    startup and render without uv, a checkout, backend environment variables or
    an existing user model cache; `scripts/build-windows.ps1` must complete from
    an ordinary account without Developer Mode. Retain checksum-bound self-test
    reports for both platforms.

## P2 - Deferred audio experiments

These tasks are useful but do not block the current Character Story release.

- [ ] Build one real blind long-pause comparison only after a new exact long-line
      raw capture contains one uniquely safe removable span. Compare independent
      sentence segmentation with center-only silence compression under identical
      text, speaker and controls; publish raw/transformed hashes and the transform
      ledger. Do not weaken the transform, reconstruct old evidence, spend
      another seed or raise the audio limit without a new bounded hypothesis and
      explicit authorization.
  - [ ] If human review selects a repair, integrate it into pregeneration with
        per-WAV transform provenance, immediate raw/repaired replay and rejection
        evidence retention. Never auto-approve transformed audio.
  - [ ] Only after the offline gate passes, reuse the same classifier and safe
        segmentation in live mode between cancellation/staleness guards.
- [ ] Evaluate typed non-verbal events on a CUDA host with official
      MOSS-SoundEffect v2 in its isolated environment. Use a fixed
      isolated-effect corpus and multiple checksum-bound seeds; record model,
      prompt, requested/actual duration, latency, VRAM, unwanted speech,
      artifacts and adherence. Require technical and blinded perceptual approval
      before adding a provider. Keep unproven original cues unbound and unsupported
      effects as explicit omissions, never silent drops.
- [ ] When CUDA is available, compare MOSS Delay 8B against MOSS Local 4B on the
      installed checksum-bound 46-line corpus. Preserve group identities, WAV
      hashes, timing/RTF, silence/quality and hardware/model provenance; compare
      only against MOSS in a new bounded blind task. Do not rerun the completed
      Local-4B/XTTS comparison or use the current CPU-only path.
- [ ] Complete the perceptual model gate before integrating another production
      speech backend. On target Windows CPU/CUDA hardware benchmark XTTS,
      Chatterbox Nano and Chatterbox Turbo for latency, realtime factor, speaker
      similarity, hallucinations, RAM/VRAM and package size. Keep F5-TTS as a GPU
      comparison rather than the initial live backend.
- [ ] Validate optional RapidOCR against Tesseract in the Windows portable build
      only after the sequence-first cutover removes OCR from ordinary locked
      playback.

## P2 - Release qualification and Windows distribution

- [ ] Run real macOS and Windows soak tests covering CPU/GPU speech, animated
      scenes, rapid manual advancement, focus loss and shutdown during every
      pipeline stage. Acceptance includes a 30-minute session without buzzing or
      underruns, no stale speech/advance, cached CPU speech within 2 seconds,
      supported CUDA speech within 750 ms, and an already-visible second sentence
      within 300 ms of the first ending.
- [ ] Record Windows release evidence across Windows 11, common GPU vendors,
      multiple displays, DPI scaling, windowed/borderless modes, normal/elevated
      game processes, portable first launch and OCR-to-speech smoke tests. The elevated
      profile must send and acknowledge an auto-advance key through the
      production controller, not merely capture/OCR the fixture and invoke the
      legacy TTS engine; otherwise explicitly mark cross-integrity input as
      unsupported rather than recording a false-green result.
