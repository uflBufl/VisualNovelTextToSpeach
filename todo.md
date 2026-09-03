# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements and completed-work history in `docs/` and Git, not here.

## P1 - Qualify the implemented desktop UX program

Follow [`docs/ui-ux-review-plan.md`](docs/ui-ux-review-plan.md) and the ranked
backlog in [`docs/ui-ux-control-audit.md`](docs/ui-ux-control-audit.md).

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
- [ ] Make runtime installation automatic after the upgrades are proven.
  - First-run setup should detect platform and NVIDIA-driver availability,
    provision the appropriate locked CPU/CUDA runtime, verify it through the
    worker, remember the result and offer a clear retry. Users must not select a
    Python or CUDA wheel manually.
  - Add dependency and smoke-test coverage for every runtime to CI; keep actual
    CUDA generation on a self-hosted Windows/Linux runner and make CPU-only CI
    validate resolution plus the typed no-CUDA path.

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
