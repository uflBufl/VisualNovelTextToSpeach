# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements and completed-work history in `docs/` and Git, not here.

## P0 - Finish the sequence-first production cutover

The active Character Story pack and sequence authority are
`current-character-story-3.7-v4-semantic` and its matching 104-event plan.
Static pack completeness, generated-audio review and visible-chapter replay
coverage are complete. The operator explicitly deferred the remaining real-game
hardware pass; proceed without representing hardware playback, focus, latency
or auto-advance as verified evidence. Follow
[`docs/current-character-story-completion.md`](docs/current-character-story-completion.md)
and [`docs/sequence-first-live-reading.md`](docs/sequence-first-live-reading.md).

- [ ] Restrict full OCR and the incremental text tracker to initial anchoring,
      bounded branch disambiguation and explicit recovery. Ordinary locked
      playback must stay on lightweight visual transition/canonical confirmation
      paths, and recovery observations must not leak into a later route.
- [ ] Make routing and advancement idempotent per cursor event. Exactly one of
      original audio, generated WAV, live fallback or intentional silence may
      become terminal; repeated frames, retries, callbacks and focus transitions
      may schedule at most one acknowledged advance key and must not replay or
      skip the following event.
- [ ] Expand deterministic replay coverage for slow/paused typewriter, noisy and
      unknown nameplates, long narration, focus loss/return, choice boundaries
      and rapid manual skipping. Reuse immutable captures where available and
      synthetic timing/focus fixtures otherwise; no new listening review is
      required. Acceptance is zero wrong-line, wrong-speaker, duplicate,
      stale-route or app-skipped dialogue across the exact replay suite.

## P1 - Expand pregenerated coverage safely

Follow
[`docs/pregeneration-coverage-plan.md`](docs/pregeneration-coverage-plan.md)
and keep original audio, approved generated audio, explicit live fallback and
intentional omission as distinct terminal authorities.

- [ ] Prepare references, then generate and review the 1,220 patch-3.7
      `no_audio` lines with the approved primary and fallback policy. Preserve
      source-audio candidates, invalidate review when WAV hashes change, review
      every technical flag plus a stratified clean sample, and expand a cohort
      only when that sample exposes a substantive defect.
- [ ] Turn the existing defect-reason labels into a calibrated MOSS quality
      router. Collect enough independently reviewed examples for pacing,
      repetition, truncation, pronunciation, artifacts and speaker identity to
      reserve an untouched validation split; compare a stronger local ASR or
      forced aligner; publish a reject threshold only after measuring false
      positives and false negatives on that split. Until then keep the current
      order: safe sentence repair when eligible, one bounded provider-local
      retry, then a typed XTTS or Pocket fallback.
- [ ] Run `vntts-benchmark-tts` on the exact Rhiannon replay corpus and retain a
      checksum-bound report with fresh/cache first-chunk timing, realtime factor
      and generated/original route timing. Device underrun remains a separate
      hardware measurement.
- [ ] Run a small blind Rhiannon comparison between the stable generation
      profile, the current reference-codec roundtrip and the original extracted
      line. Judge speaker similarity, pronunciation, prosody, artifacts,
      repetition and trailing silence before changing sampling controls.

## P1 - Resolve project-wide code-review findings

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
