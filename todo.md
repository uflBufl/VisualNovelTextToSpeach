# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements and completed-work history in `docs/` and Git, not here.

## P0 - Restore Python 3.14 CI dependency sync

- [ ] Make the frozen Python 3.14 lock installable on macOS, Windows and Linux.
  - Use a matching Torch/Torchaudio release that publishes CPython 3.14 wheels
    for every target; keep the CUDA 12.9 index Linux-only because it has no
    Windows wheels for the selected release.
  - Run the Linux X11 suite under Xvfb with EGL available; `offscreen` alone
    does not provide the display required by `pynput`.
  - Install PipeWire's runtime library in Linux CI so importing Qt Multimedia
    does not emit loader failures during clean CLI help smoke tests.
  - Preserve failing macOS shard output as a GitHub annotation so CI identifies
    the exact test instead of reporting only the shard exit code, including
    both ends when a large assertion would otherwise hide the test name.
  - Isolate the nine Qt asset-manager tests from the non-Qt remainder process;
    the macOS runner exited with signal 11 immediately after that module while
    preserving exact once-only discovery coverage.
  - Accept a not-yet-created screenshot directory; capture already creates the
    full path recursively.
  - Gate: `uv sync --frozen --dry-run` resolves for Windows x64 and Linux x64,
    the local Python 3.14 quality/full-suite gate passes, and the pushed CI run
    completes successfully on all three operating systems.

## P0 - Make the Windows test gate non-admin portable

- [ ] Make the full build/test gate pass from an ordinary Windows account.
  - Replace only Windows-failing POSIX fixture paths with platform-neutral
    temporary paths after binding each change to an exact traceback; do not
    rewrite harmless virtual paths used solely as mocked values.
  - Gate: `scripts/run_ci_unittests.py discover -s tests` and
    `scripts/build-windows.ps1` complete as a non-admin user without requiring
    Developer Mode. Retain the ordinary macOS full-suite gate.

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

## P1 - Complete distributable release packages

- [ ] Make release packages able to run the backend they recommend by default.
  - Follow [`docs/release-speech-runtime.md`](docs/release-speech-runtime.md).
    Retain repository/revision/checksum evidence for every downloaded model and
    voice. Do not bundle gated weights or unclear/non-commercial voices without
    a release-owner approval covering those exact files.
  - Complete the Developer ID signed/notarized macOS build plus the Windows
    portable and installer builds before removing this item. Acceptance requires
    startup and render without uv, a checkout, backend environment variables or
    an existing user model cache; retain checksum-bound self-test reports for
    both platforms.

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
      MOSS-SoundEffect v2 in its separate Python 3.12 environment. Use a fixed
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
- [ ] Create and sign a Windows installer with Start Menu shortcuts, optional
      startup, upgrades and clean uninstall. Preserve downloaded models and user
      settings across upgrades.
- [ ] Record Windows release evidence across Windows 11, common GPU vendors,
      multiple displays, DPI scaling, windowed/borderless modes, normal/elevated
      game processes, installation and OCR-to-speech smoke tests. The elevated
      profile must send and acknowledge an auto-advance key through the
      production controller, not merely capture/OCR the fixture and invoke the
      legacy TTS engine; otherwise explicitly mark cross-integrity input as
      unsupported rather than recording a false-green result.
