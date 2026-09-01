# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements and completed-work history in `docs/` and Git, not here.

## P0 - Make pregeneration self-service

Follow
[`docs/self-service-pregeneration.md`](docs/self-service-pregeneration.md).
Pregeneration is an ordinary-user workflow: a player selects installed story
content, confirms only a small number of ambiguous character voices, and lets
VNTTS build and activate a local game pack without exposing authoring concepts.

- [ ] Refine ambiguous-voice comparison without expanding mandatory review.
  - [ ] Calibrate an obvious-artifact preview rejection rule against held-out
        decisions. Existing exact-repeat, near-clipping and discontinuity signals
        remain diagnostic because the current labelled corpus does not establish
        a safe automatic margin.
## P1 - Improve the self-service generation engine

Follow
[`docs/pregeneration-coverage-plan.md`](docs/pregeneration-coverage-plan.md)
and keep original audio, approved generated audio, explicit live fallback and
intentional omission as distinct terminal authorities.

- [ ] Turn the existing defect-reason labels into a calibrated MOSS quality
      router. Collect enough independently reviewed examples for pacing,
      repetition, truncation, pronunciation, artifacts and speaker identity to
      reserve an untouched validation split; compare a stronger local ASR or
      forced aligner; publish a reject threshold only after measuring false
      positives and false negatives on that split. Until then keep the current
      order: safe sentence repair when eligible, one bounded provider-local
      retry, then a typed XTTS or Pocket fallback.
- [ ] Validate speaker-identity clustering before reducing voice choices. Select
      a redistributable local speaker-embedding implementation and label held-out
      same-speaker, different-speaker and same-character/different-age pairs from
      installed references. Publish a threshold only if it preserves every known
      age/identity boundary; until then keep the current evidence variants apart.
- [ ] Run a small blind Rhiannon comparison between the stable generation
      profile, the current reference-codec roundtrip and an original extracted
      line. Include the exact expressive-profile recovery for the no-audio short
      hesitation against the ordinary Pocket fallback; stable seeds 0-2 and
      natural seed 0 all miss EOS for that line. Judge speaker similarity,
      pronunciation, prosody, artifacts, repetition and trailing silence before
      changing sampling controls or automatically escalating the MOSS profile.

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
