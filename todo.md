# TODO

Keep this file limited to actionable, unfinished work. Put durable decisions,
measurements in agent memory and completed-work history in Git, not here.

Execution labels:

- **Ready**: current code path and intended behavior are understood; implement after
  this plan is accepted.
- **Investigate**: add or use bounded diagnostics and reproduce before choosing a
  fix; do not patch the observed symptom.
- **Validate**: implementation exists or depends on a real host; no speculative
  code changes before the stated run.

Planned implementation order after approval:

1. Reproduce and fix the Windows device-quiescence race, then use the same
   timelines to resolve duplicate playback, ellipsis loss and avoidable story
   misses.
2. Extend the voice-plan decision contract, then implement the optional
   pre-generation reference inspector without turning automatic choices into
   mandatory review.
3. Instrument the current planning/finalization paths and optimize only work that
   the new measurements prove is still duplicated.
4. Run platform, hardware and signing gates only when the required host or
   credentials are available.

## P0 - Play while offline audio is still preparing

- [ ] **Validate in a sustained chapter:** an occurrence is now claimed as soon as
      its first PCM is emitted, even if playback is later interrupted. Sequence
      leases and non-sequence generation sealing suppress a second route without
      deduplicating by text or line ID, so genuinely repeated dialogue remains
      playable. Re-run the captured lines `314601:46`, `:50`, `:67`, `:68`, `:70`,
      `:71`, `:75` and `:76`; require zero duplicate starts and at most one audible
      route per occurrence.
- [ ] **Validate in the affected chapter:** the existing visual ellipsis detector,
      punctuation-only capture classification and cursor-owned silent route already
      cover omitted sequence `314601:78`, including one guarded auto-advance and no
      MOSS request. Confirm `...`, `…` and surrounding whitespace produce one silent
      outcome, manual mode dispatches no key, and `Wait...` remains speech. If the
      failure recurs, collect the support archive to identify which already logged
      boundary rejected the silent event before changing code.
- [ ] **Investigate from one fresh support archive:** `story-line-no-match` now
      records normalized OCR text/speaker hashes, lengths, eligible and
      speaker-matching candidate counts, missing identities, best bounded evidence
      and a concrete rejection reason without storing dialogue. Reproduce the ten
      prepared-story misses, classify each rejection, then fix only the proven
      boundary. Gate: every captured miss has a reason, valid candidates use the
      prepared WAV, and only proven out-of-pack lines reach live TTS.
- [ ] **Validate on Windows:** resume the supplied 682-line story with the recovery
      fix and confirm that the two short `I ...` lines are repaired while the two
      exhausted MOSS lines become live fallbacks without aborting pack publication. The latest
      state has 580 generated and four failed items: the two Aderyn failures share
      a two-character transformed input and were rejected only for 0.64 seconds of
      trailing silence in a 1.12-second WAV; Poacher I hit the 8.5-second limit and
      Poacher II hit the 3-second limit. Trim or tolerate safe edge silence for the
      short utterances and publish explicit live fallbacks for exhausted limits.
- [ ] **Validate after the live fixes:** verify with one selected multi-chapter
      story: start after a partial chapter, continue generation while prepared
      WAVs play, cross a newly published boundary without duplicate speech, wait
      safely when catching the worker, resume after success, survive
      cancellation/restart, and finish with the same validated pack as
      uninterrupted offline preparation.

## P0 - Validate the non-blocking Voice plan

- [ ] **Validate Mrs. Owen on fresh Windows state:** after updating the extractor,
      open her voice from both Voices and Stories. Both must expose the same
      checksum-bound 3.17-second media `562400954` and 1.95-second media
      `599773947`, without duplicate quoted role labels. Play the original,
      generate a preview, save the 3.17-second candidate, reopen, and prepare a
      story. Gate: the saved choice remains selected and offline generation uses
      that exact reference; no automatic choice silently replaces it.
- [ ] **Validate the Windows preview startup repair:** Python 3.14 `Popen` does not
      retain a `_thread` handle, so the suspended owned MOSS process failed before
      model loading. The launcher now finds and resumes the process thread through
      documented Win32 Tool Help APIs after binding the process to its kill-on-close
      Job Object. Gate: `Inspect selected voice` loads MOSS, produces a preview,
      remains reusable for a second preview, and forced VNTTS termination leaves no
      owned `moss-tts-server.exe` process.
- [ ] **Needs player validation:** prepare one story with an automatically matched
      character, one ambiguous character and one narrator fallback. Confirm that
      generation can start without reviewing them; the Voice plan lists all roles
      in useful order; `Inspect selected voice` plays the original and the exact
      production preview; `Another phrase`, `Use selected reference`, `Use narrator`,
      `Keep automatic choice`, and `Change selected voice...` are clear;
      and an explicit choice survives reopening while an untouched recommendation
      remains automatic.

## P0 - Stabilize Windows audio and the OpenMOSS runtime

- [ ] **Validate on Windows:** the captured `wdmaud.drv`/PortAudio teardown race
      now has owner-thread-only stream teardown, cancellable 50 ms PCM writes and a
      bounded reader quiescence barrier covering capture, OCR, synthesis and
      playback before restart or shutdown. Stress focus loss plus stop/restart at
      short-stream completion. Gate: no native crash, stale audio or overlapping
      output stream; the support archive records matching stream owner and lifecycle
      events.
- [ ] **Validate on Windows:** managed OpenMOSS now starts suspended, joins a private
      Job Object with kill-on-close, and only then resumes. Run the Windows-only
      subprocess crash test and force-close VNTTS during one real render. Gate: the
      owned `moss-tts-server.exe` process tree disappears while an independently
      started server is never terminated.
- [ ] **Validate on Windows:** run the qualified GPU/8 profile beside Reverse: 1999
      and confirm game-time VRAM headroom and responsiveness before making it the
      accelerated default. Retain CPU/4 as the fallback; extra CPU workers did not
      improve warm renders consistently on the qualified host. The captured GPU/4 run used 37 Vulkan
      layers but native telemetry reported no GPU-board samples, about 11.0 GB peak
      native RSS, about 2.1 GB host RSS and about 5.8 GB minimum free RAM; fix GPU
      telemetry and measure the real game-plus-render headroom rather than inferring
      acceleration from configuration.

## P1 - Reduce preparation and post-generation saving latency

- [ ] **Validate finalization on Windows:** repeat the 1,071-file story that took
      73.8 seconds after recovery (acceptance 33.14, publication 38.09,
      activation 2.29). Inspect the new `pregeneration-acceptance-*` and existing
      `pregeneration-publication-*` phases from the last generated line to usable
      audio. Require identical published bytes and corruption failures, with
      faster cold publication and unchanged resume. If a long phase remains,
      measure its file counts/bytes before changing another validation boundary;
      expose the dominant phase in the UI.
- [ ] **Ready after the profile:** classify every finalization check by the invariant
      it protects. Remove checks already proven by an unchanged checksum-bound
      generation state; retain trust-boundary and corruption checks. Reuse one
      immutable validation result within the same transaction and an already
      published checksum identity across activation, but do not infer unchanged
      content from weak file metadata or skip the deep publication boundary.
- [ ] **Ready after the optimization:** add a regression benchmark for cold
      finalization and an unchanged resume. Require identical published pack
      contents and failure behavior, while telemetry proves there is no duplicate
      read/decode/hash of the same WAV inside one transaction and the unchanged path
      completes within a small, measured bound instead of minutes.

## P1 - Measure remaining player latency

- [ ] **Profile remaining Voice plan work:** on the local 253 MB reference index,
      the new checksum-bound role cache reduced repeated `_candidate_roles` from
      9.18 to 0.37 seconds. Collect the new candidate-preparation/store phase logs
      in a real plan and count candidate WAV reads/hashes, `VoiceLibrary.discover`
      writes and `bindings()` reads. Gate: phase timings sum to the outer wait;
      remove only measured duplicate work while preserving checksums, saved choices
      and identical plans on cold/repeat runs.
- [ ] **Profile live capture on representative game frames:**
      `live.py:_run_capture` calls the dialogue and render fingerprints, presence
      and completion checks on each capture. On a synthetic 1280x320 frame their
      medians totaled about 30 ms, with 21.6 ms in
      `fingerprint_dialog_frame`. Measure per-stage p50/p95 and CPU on animated,
      static and typewriter frames at the actual capture interval. Optimize the
      dominant image pass only if material; gate on unchanged frame routing,
      ellipsis detection, auto-advance and no duplicate speech in replay.
- [ ] **Benchmark OCR subprocess work on captured dialogue:**
      `ocr.py:recognize_dialog_image_result` tries up to three profiles, each
      invoking Tesseract multiple times for speaker and dialogue. Record p50/p95,
      profile attempts and process calls on a fixed image corpus using
      `vntts-benchmark-ocr`; try fewer passes only if they preserve speaker/text
      accuracy, confidence handling and the current uncertain-frame behavior.

## P1 - Qualify remaining Python and speech runtimes

- [ ] **Validate on a Windows CUDA host:** qualify the Python 3.14 MOSS Delay
      candidate with its atomic Torch/TorchAudio/TorchCodec stack and a supported
      shared FFmpeg build. Require CUDA import, one checksum-bound reference-conditioned render,
      finite audio, clean shutdown and recorded RAM/VRAM/latency. CPU-only hosts
      must return a clear unsupported-backend result instead of loading the 8B
      model.
- [ ] **Ready after the production MOSS qualification:** make runtime preparation
      select only a qualified stack from detected hardware. Exercise absent runtime,
      interrupted download, retry and restart on clean macOS and Windows. Users
      must not choose Python or CUDA wheels manually.

## P1 - Complete distributable packages

- [ ] **Validate on clean machines:** make the macOS and Windows portable packages
      start and render with their recommended backend without uv, a checkout,
      environment variables or an existing model cache. Keep model/license consent
      separate from dependency setup and verify every downloaded runtime artifact by checksum.
- [ ] **External credentials and validation:** complete a Developer ID
      signed/notarized macOS package and signed Windows executable. The Windows
      build must complete from an ordinary account without Developer Mode. Retain
      checksum-bound startup/render reports for both platforms. Add a release-
      promotion gate that accepts only the signed archive after every required
      Windows hardware profile passes the existing
      matrix validator without `--allow-unsigned`.

## P2 - Check smaller repeated reads

- [ ] **Measure prepared WAV preflight during Reading:**
      `GeneratedAudioLibrary.find_with_preflight` reads and hashes the full WAV
      before consulting its decoded-audio cache. Record file sizes, lookup p95,
      cache hits and first-PCM delay on repeated and distinct lines. Change reuse
      only if this is material and every played byte remains checksum-verified.
- [ ] **Measure repeated startup discovery:** `find_default_voice_manifest`
      reparses the manifest and checks each reference on every call; story discovery
      hashes each candidate even when its catalog is cached. Count calls and bytes
      in one startup and one reopen of Voices/Stories (the local two-index disk-cache
      discovery took 0.49 seconds). Remove proven redundant passes while keeping
      content-based invalidation; file size or mtime alone is not integrity proof.

## P2 - Qualify the real desktop experience

- [ ] **Validate on clean machines:** on clean macOS and Windows installs,
      auto-detect the game (with folder fallback), select a game narrator, preview
      and save it, restart, then verify that live fallback and offline preparation
      both retain the choice.
- [ ] **Validate manually:** exercise the main player journeys on real displays:
      fullscreen and multi-monitor use, 100%/150%/200% scaling, VoiceOver/Windows
      Narrator, focus loss, rapid advancement and shutdown during each pipeline stage.
      Check Stories, Voices, Reading, setup and Settings for visible primary
      actions at small sizes, exact clipboard copying, keyboard navigation and
      contrast when switching system light/dark themes while the app is open.
- [ ] **Validate manually:** run a 30-minute macOS and Windows soak covering CPU/GPU
      speech and animated scenes. Require no buzzing, underruns, stale speech or
      stale auto-advance; record hardware and timing evidence instead of relying
      on subjective status.

## P2 - Optional model experiments

- [ ] **Optional validation:** on a CUDA host, compare MOSS Delay 8B with MOSS Local
      4B on the existing checksum-bound 46-line corpus. Preserve group identities,
      WAV hashes, timing/RTF, quality signals and hardware/model provenance; do not repeat the
      completed Local-4B/XTTS comparison.
- [ ] **Optional validation:** evaluate typed non-verbal events with official MOSS
      SoundEffect v2 using a small fixed corpus and multiple checksum-bound seeds.
      Require technical and blinded perceptual approval before adding a provider;
      unsupported effects remain explicit omissions.

## P2 - Consolidate authoring staging

- [ ] Share the base workspace staging steps used by audio-event omission and
      spoken-projection fallback. Keep their terminal state mutations and
      validators separate. Gate: repeat-call and changed-authority tests pass.
