# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements in agent memory and completed-work history in Git, not here.

## P0 - Prefer spoken playable-character narrator references

- [ ] Verify on Windows that Centurion's references are spoken playable dialogue
      matching the displayed transcripts, or that absent playable speech is
      explained without offering story effects. Confirm downloaded story voices
      remain available alongside packaged voices.

## P0 - Redesign the ordinary-player interface

Priority: redesign the experience before strengthening end-to-end UI tests.
Scope is the main application, first launch, game import, voice selection,
story preparation and reading; not specialist authoring/review tools. Existing
control-level audit completion does not mean the overall journey is satisfactory.
The user approved implementation. Next consolidate the Voices editor and
simplify the preparation form inside Stories without replacing the existing
generation/playback services. Finish contextual reading setup rather than
adding another wizard or library implementation.

- [ ] Refine the target journey and screen layouts while integrating services.
  - Use one main window with Stories, Voices and Reading sections and one shared
    selected-game context. Settings/support are secondary actions, not extra
    setup workflows. Default a new install to Stories; return an existing user
    to the last relevant section without automatically starting playback.
  - Produce reviewable layouts for the three sections with realistic short/long
    content and empty, loading, ready, partial and error states. Specify primary
    actions, back/cancel behavior and what persists across navigation.
  - Keep one visible task/action hierarchy and consistent spacing, typography,
    status vocabulary, button positions and preview transport. No new design
    system, web frontend or general navigation framework.
- [ ] Replace the up-front technical wizard with contextual setup.
  - First launch asks for an installed game, with automatic discovery and one
    folder fallback. Import exposes progress and populates a persistent story
    library; it must not require a running game, calibrated OCR or a loaded TTS.
  - Load expensive speech/runtime dependencies only for preview, preparation or
    reading as needed. Browsing stories/settings must not wait for model startup.
  - Offer the recommended supported speech engine with an understandable reason;
    show its actual model and readiness. Keep alternate engines available without
    exposing Python/CUDA/raw manifest fields in the ordinary path. Never silently
    change a chosen game voice into a built-in voice to bypass missing setup.
  - Request model download/access/license only at the action needing it. Show
    what is missing and its direct remedy; retain consent and access boundaries.
  - On first Start reading, guide game-window selection, necessary OS permissions,
    capture-area confirmation and a short reading check in context; do not force
    the entire setup sequence again after successful configuration.
- [ ] Make Stories the preparation and readiness home.
  - List all imported stories with search/filter, persisted selection and explicit
    status: not prepared, preparing, partially prepared, ready, or needs attention.
    Opening a story shows original/prepared/live-TTS/omitted coverage with counts
    scoped to that story; never call a live-dependent result fully offline-ready.
  - Use one primary action appropriate to the selected story: Prepare, Continue
    preparation or Start reading. Keep multi-story preparation as selection in
    the same library, not another dialog or a separate queue product.
  - Before preparing, show one concise summary: selected stories, narrator,
    engine/model, character-voice exceptions and approximate work/storage. Link
    to the shared Voices editor instead of embedding a second settings form.
- [ ] Consolidate all ordinary voice selection into one Voices editor.
  - Separate narrator from character roles, using one shared editor/player from
    every entry point. Clearly distinguish game voices and built-in voices;
    show the selected source character, original sample and generated preview.
  - Keep play/replay/stop in fixed positions. Explain when the app is loading a
    reference versus generating a preview; changing a candidate never silently
    applies it. Use an explicit save action and preserve the previous selection
    on cancel. Render available portraits rather than showing image paths.
  - Show automatic character assignments and only exceptional choices prominently.
    Reuse accepted choices; optional listening to a few voices must suffice.
    Do not require line/cohort reviews or ask users to approve known failures.
  - Make fallback policy explicit before preparation/reading: which roles will use
    narrator or live speech and whether the speaker name is announced (`???` is
    announced as Unknown). Do not introduce routine blocking prompts mid-reading.
  - Define changes as defaults for future preparation, not retroactive changes to
    existing WAVs. Show affected stories and offer scoped regeneration; preserve
    unrelated recordings and display their actual recorded voice/model provenance.
- [ ] Put long-running work and recovery in a consistent visible location.
  - Keep one compact task strip/card visible across main sections: operation,
    phase, completed/total, saved progress, and Cancel/Continue where supported.
    Show indeterminate progress honestly when totals/ETA are unknown.
  - Disable only actions that conflict with the active operation and explain why
    beside them. Keep safe navigation available; switching sections cannot cancel
    work or lose its state. Define close/minimize/quit consequences explicitly.
  - Hide editing-only license/settings forms while generation runs; foreground
    progress and the final original/prepared/live-TTS coverage instead.
  - Give errors a plain-language cause and next action in place; technical logs
    are secondary. No success-looking partial failures or unexplained disabled
    controls. Reuse existing cancellation/stale-result guards and durable jobs.
- [ ] Simplify Reading and align it with the chosen story and voices.
  - Show current story/position, dialogue speaker, actual playback voice and
    source (original game audio, prepared recording or live generation). Keep
    recording engine/model distinct from the engine configured for live fallback.
  - Keep one Start/Stop reading action and a stable Pause/Replay/Skip transport;
    retain a clearly separated emergency stop. Calibration, OCR internals and
    manual resync belong to contextual recovery or details, not the main hierarchy.
  - Derive compact/tray controls from the same actions/state; do not duplicate
    independent setup or voice-selection workflows there.
- [ ] Implement in reviewable slices after the design gate: shell/library and
      deferred setup; shared Voices; preparation/progress/activation; Reading.
  - Reuse current importer, generation stores, controller, voice binding and
    playback services. Move only necessary UI-owned task lifetime into the main
    application so navigation is safe; do not rewrite the synthesis pipeline.
  - Visually inspect each slice at normal/minimum sizes and enlarged text, with
    keyboard navigation and slow/error states. Preserve data safety and existing
    checks; add only focused checks needed for changed behavior, not the deferred
    end-to-end expansion below.
  - Acceptance: the user can tell what is selected, what is running, what is
    ready and what to do next without logs; can prepare before launching the game;
    and can read with the chosen voices without repeated setup/ordinary review.

## P1 - Diagnose intermittent offscreen Qt test stalls

- [ ] Capture thread stacks when the macOS `qt-app` shard stalls in
      `test_settings_are_scrollable_and_grouped_into_visual_regions` (180-second
      timeout). It passes in isolation and the repeated shard also passes;
      identify the leaked event/modal/worker state before changing timeouts.

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

## P0 - Qualify game-audio decoder provisioning

- [ ] Confirm the hosted Windows/Linux automatic-download and native-decode
      checks pass; macOS source and relocated native bundle probes passed locally.

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
