# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements in agent memory and completed-work history in Git, not here.

## P1 - Consolidate atomic workspace publication

- [ ] Inventory the 41 `tempfile.mkdtemp` publication sites in 31 production
      modules and classify atomic publishers, disposable previews and directories
      whose lifetime intentionally escapes the call. Migrate only the first two
      classes to the existing `publication.staged_directory`; preserve exact
      destination-conflict, retry and cleanup behavior.
- [ ] Pilot the shared publication lifecycle on
      `reviewed_rejection_fallback.py`, `reviewed_waveform_publication.py`,
      `audio_event_projection_fallback.py` and
      `known_role_live_fallback.py`. Keep domain validation in each publisher;
      centralize the repeated staging, lease/recheck, no-replace commit and
      cleanup sequence only where it remains identical after the pilot.
- [ ] Migrate the remaining qualified publishers in small domain groups. For
      every group, test a source-digest race, an existing destination, an
      exception during staging and removal of abandoned staging directories,
      then run its focused authoring tests and the full suite.

## P1 - Expand static typing by coherent boundaries

- [ ] Establish the all-production mypy inventory as a shrinking baseline. The
      current strict scan reports 3,568 errors across 207 files, including 3,562
      `no-untyped-def` errors, while `pyproject.toml` checks only 19 files. Make
      `check_mypy_scope.py` retain every newly completed module and reject scope
      loss without accepting new `Any`, `cast` or blanket ignores as completion.
- [ ] Type the small shared foundations first: `path_safety.py`,
      `application_directories.py`, `document_identity.py`, `versioned_json.py`,
      `hotkeys.py`, `dialog.py` and `auto_advance_policy.py`. Add each passing
      file to the configured scope and baseline in the same change.
- [ ] Type the persisted authoring contract boundary through
      `generation_state.py`, `reconciliation_schema.py`, `workspace_state.py`
      and `queue_extension.py`, then their nearest bulk-generation, workbench and
      terminal-conflict consumers. Preserve optionality, validation order and
      existing error text for stored JSON; run the focused reconciliation,
      generation-state, terminal-conflict and workbench tests after each slice.
- [ ] Type the runtime boundary through `voices.py`, `runtime_config.py`,
      `speech_worker.py`, `speech_backend.py`, `controller_components.py` and
      `controller.py`. Replace the broad `Any` bridge in the already-scoped
      `controller_components.py` with concrete existing runtime types as its
      owners become typed; keep backend protocols limited to multiple real
      implementations.
- [ ] Type Qt/UI modules only after their service contracts are checked. Work
      screen by screen so signal payloads, optional widget state and worker
      results become explicit without introducing parallel view-model layers.

## P1 - Reduce structural complexity at lifecycle owners

- [ ] Restore the complexity ratchet before taking a new baseline. It currently
      rejects five findings in `workbench._load_workspace_scoped`,
      `workbench._validate_workspace_offline_fallback_state` and
      `workspace_state.load_stable_workspace_generation_state`; remove those
      regressions rather than adding allowances.
- [ ] Split `authoring/workbench.py` along existing ownership boundaries. It is
      6,389 lines with 140 top-level functions and 38 production consumers:
      migrate foundation/path/authority callers to the existing focused modules,
      then isolate workspace creation, inspection, carry-forward and merge
      phases while retaining only the compatibility imports that still have
      consumers. Check importability and focused tests after every move.
- [ ] Decompose the high-branch authoring workflows one lifecycle family at a
      time: workspace/carry-forward validation, generation-state record
      validation, source-reference publication and bulk review/fallback. Extract
      phase helpers with domain names, preserve validation and lease order, and
      delete each matching C901/PLR0912/PLR0915 baseline entry as it disappears.
- [ ] Break the seven largest Qt constructors into named section builders,
      signal wiring and initial-state methods, starting with
      `pregeneration_ui.py` and `authoring/workbench_ui.py` (558 and 555 lines),
      then the large constructors in `app.py`, `cohort_bundle_ui.py`,
      `dashboard_ui.py`, `onboarding_ui.py` and `game_narrator_ui.py`. Reuse the
      current widgets and layouts; do not add a UI framework or generic factory.

## P1 - Unify background UI task ownership

- [ ] After fixing onboarding cleanup, move result-only diagnostics and asset
      checksum verification from raw daemon `Thread` launches to the existing
      `LatestTaskRunner`, with tests proving stale completions and completions
      after dialog close cannot update the UI.
- [ ] Give asset download and support export explicit lifecycle contracts before
      sharing their runner: either cooperative cancellation through the blocking
      operation or a tested finish-in-background policy. Preserve progress and
      error delivery, and remove the raw-thread paths only after close/cancel
      tests pass.

## P1 - Make debt tooling trustworthy

- [ ] Establish dependency vulnerability evidence for the root and every backend
      lock without adding a new dependency first. Triage the current locks with
      an existing advisory service/tool, record actionable advisories, and add a
      CI gate only if it covers pinned Git dependencies and produces stable,
      suppressible results.
- [ ] Investigate uv's invalid `>= '2.7'` metadata warning from the pinned
      Chatterbox dependency. Keep the current pin while frozen sync and platform
      smoke pass; move to a corrected upstream commit only after Windows and
      Linux Chatterbox smoke tests succeed.

## P0 - Reduce OpenMOSS generation latency

- [ ] Run the qualified GPU/8 profile beside Reverse: 1999 and confirm game-time
      VRAM headroom and responsiveness before making it the accelerated default.
      Retain CPU/4 as the fallback; extra CPU workers did not improve warm renders
      consistently on the qualified host.
- [ ] Evaluate multiple native servers only on hosts with multiple GPUs or
      measured RAM/VRAM headroom. Keep one serialized server as the default.

## P1 - Qualify remaining Python and speech runtimes

- [ ] Qualify the Python 3.14 MOSS Delay candidate on Windows CUDA with its atomic
      Torch/TorchAudio/TorchCodec stack and a supported shared FFmpeg build.
      Require CUDA import, one checksum-bound reference-conditioned render,
      finite audio, clean shutdown and recorded RAM/VRAM/latency. CPU-only hosts
      must return a clear unsupported-backend result instead of loading the 8B
      model.
- [ ] Qualify Python 3.14 MOSS SoundEffect v2 on Linux CUDA with one fixed-seed
      render and clean shutdown. If the model stack fails under NumPy 2, retain
      Python 3.12 for this isolated runtime and record the exact incompatibility.
- [ ] After those real-render gates pass, make runtime preparation select only a
      qualified stack from detected hardware. Exercise absent runtime,
      interrupted download, retry and restart on clean macOS and Windows. Users
      must not choose Python or CUDA wheels manually.

## P1 - Complete distributable packages

- [ ] Make the macOS and Windows portable packages start and render with their
      recommended backend without uv, a checkout, environment variables or an
      existing model cache. Keep model/license consent separate from dependency
      setup and verify every downloaded runtime artifact by checksum.
- [ ] Complete a Developer ID signed/notarized macOS package and signed Windows
      executable. The Windows build must complete from an ordinary account
      without Developer Mode. Retain checksum-bound startup/render reports for
      both platforms. Add a release-promotion gate that accepts only the signed
      archive after every required Windows hardware profile passes the existing
      matrix validator without `--allow-unsigned`.

## P1 - Preserve diagnostics across crashes

- [ ] Persist the bounded, sanitized OpenMOSS native event log and reload it on
      the next launch so an exported support bundle can explain a crash or forced
      exit. Keep audio, dialogue text, local paths and unbounded logs excluded.

## P2 - Qualify the real desktop experience

- [ ] On clean macOS and Windows installs, auto-detect the game (with folder
      fallback), select a game narrator, preview and save it, restart, then verify
      that live fallback and offline preparation both retain the choice.
- [ ] Exercise the main player journeys on real displays: fullscreen and
      multi-monitor use, 100%/150%/200% scaling, VoiceOver/Windows Narrator,
      focus loss, rapid advancement and shutdown during each pipeline stage.
      Check Stories, Voices, Reading, setup and Settings for visible primary
      actions at small sizes, exact clipboard copying, keyboard navigation and
      contrast when switching system light/dark themes while the app is open.
- [ ] Run a 30-minute macOS and Windows soak covering CPU/GPU speech and animated
      scenes. Require no buzzing, underruns, stale speech or stale auto-advance;
      record hardware and timing evidence instead of relying on subjective status.
- [ ] Restore reliable macOS-native global controls for pause, skip, replay and
      emergency stop, or make the compact controls the explicit gameplay handoff
      until those shortcuts are available.

## P2 - Handle in-game choices without mode babysitting

- [ ] Detect a Reverse: 1999 choice/manual boundary outside the dialogue OCR
      region, pause auto advance without losing the story cursor, and resume
      ordinary reading after the player chooses. Do not require the player to
      predict a choice and toggle live reading manually.

## P2 - Optional model experiments

- [ ] On a CUDA host, compare MOSS Delay 8B with MOSS Local 4B on the existing
      checksum-bound 46-line corpus. Preserve group identities, WAV hashes,
      timing/RTF, quality signals and hardware/model provenance; do not repeat the
      completed Local-4B/XTTS comparison.
- [ ] Evaluate typed non-verbal events with official MOSS SoundEffect v2 using a
      small fixed corpus and multiple checksum-bound seeds. Require technical and
      blinded perceptual approval before adding a provider; unsupported effects
      remain explicit omissions.
