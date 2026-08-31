# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements and completed-work history in `docs/` and Git, not here.

## P0 - Close the semantic-v4 live acceptance gate

The active Character Story pack and sequence authority are
`current-character-story-3.7-v4-semantic` and the matching 104-event plan.
Static pack completeness, generated-audio review and full visible-chapter replay
coverage are already complete; do not repeat those authoring reviews. Follow
[`docs/current-character-story-completion.md`](docs/current-character-story-completion.md)
and
[`docs/sequence-first-live-reading.md`](docs/sequence-first-live-reading.md).

- [ ] Run one focused real-game regression pass after restarting the application:
  - Verify mixed-rate generated playback on lines
    `reverse1999:314601:6` and `reverse1999:314601:8`: pitch and duration must
    match direct WAV playback, with no distortion, duplicate playback or
    underflow. If device format still differs, fail the route with a diagnostic
    and permit one clean stream reopen; do not regenerate a checksum-valid WAV.
  - Recheck continue-indicator handling on line `:20`, stable-frame recovery on
    `:28`, `:36` and `:83`, unknown/noisy nameplates on `:52` and `:58`, the
    short generated line `:57`, and line-less silent event 78. Acceptance
    requires one route and at most one focus-owned key per event, no key while
    glyphs are changing, no duplicate Pocket fallback, and no voice-decision
    prompt during locked sequence playback. `???` must remain `Unknown`;
    transient nameplate fragments such as `WP` must remain OCR noise, and event
    78 must schedule no speech. Preserve the exact diagnostic crop if a line
    stalls again.
  - Exercise focus loss/return and one real choice or manual boundary.
    `audio-auto` must pause without sending a key to an unfocused window, resume
    cleanly after focus returns, and stop at a choice rather than infer a path.
  - Enable `Narrator fallback roles only` for the pass. Named fallback and
    `Unknown` announcements must distinguish narrator-voiced characters from
    true Narrator/Centurion lines without becoming repetitive or changing
    auto-advance.
- [ ] Retain the privacy-safe timeline from that pass and evaluate the actual
      latency boundary. Separate typewriter/recognition, canonical confirmation,
      source cue, announcement, WAV lookup/decode, queue wait and first device
      write. Pregenerated speech should normally start during rendering or no
      later than 250 ms after the last glyph; intentional source cues and speaker
      announcements are not WAV-loading delay.
- [ ] Validate Pocket's 250 ms stream prefill and MOSS's selected
      4-frame/0.25-second stream on real output hardware. Record first PCM,
      realtime factor and underruns; acceptance is no audible buzzing and zero
      underruns in the focused pass.

## P0 - Finish the sequence-first production cutover

This work starts only after the real-game gate above passes.

- [ ] Make `audio-auto` the normal sequence-plan path while retaining
      `audio-manual` as the explicit recovery mode. Keep focus loss, choices,
      ambiguity, unsupported events and desynchronization fail-closed.
- [ ] Remove OCR text and OCR-derived speakers from locked-mode speech routing.
      Retire the incremental tracker to bootstrap, branch disambiguation and
      recovery only. Acceptance requires zero wrong-line, wrong-speaker,
      duplicate or app-skipped dialogue and exactly one confirmed key per
      eligible cursor event in the reviewed replay corpus and a real-game pass.
- [ ] Extend the captured replay corpus with real slow/paused typewriter,
      nameplate noise, long narration, branch, focus-loss and rapid-manual-skip
      frames. Generated-successor preflight may reserve only one exact generated
      successor and must never start live TTS from a prefix or leave a stale
      route after cursor, focus, pack, backend or checksum changes.

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
    Keep the safe download-on-first-run policy until a release owner explicitly
    approves redistribution of the exact pinned gated weights and allowlisted
    voice files; do not claim an offline bundled model before that gate passes.
  - Run the Pocket lock through `vntts.release_runtime` on macOS and Windows:
    stage an adjacent managed CPython plus a fresh `uv venv --relocatable`, then
    require its copied-to-a-new-path provenance probe to pass. Include that exact
    staging tree in the bundle; never rely on a developer Python, checkout or
    backend environment override.
  - Limit frozen Settings/onboarding choices to backends whose runtime contract
    the package actually supplies; preserve explicit existing user settings but
    surface a direct remediation instead of recommending an absent backend.
  - Extend package self-test to initialize the effective clean-install default
    and render non-empty PCM. Verification must clear development PATH/Python,
    backend overrides and model caches, and assert interpreter, module and model
    origins remain inside the extracted package.
  - Complete real unsigned and signed macOS builds plus the Windows portable and
    installer builds before removing this item. Acceptance requires startup and
    render without uv, a checkout, backend environment variables or an existing
    user model cache; retain checksum-bound self-test reports for both platforms.

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
