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

1. Establish one strict source-audio authority contract and one non-blocking live
   voice-resolution path, including audible narrator-fallback announcements.
2. Add session identity, sanitized previous-session preservation and audio
   lifecycle telemetry.
3. Reproduce and fix the Windows device-quiescence race, then use the same
   timelines to resolve duplicate playback, ellipsis loss and avoidable story
   misses.
4. Extend the voice-plan decision contract, then implement the optional
   pre-generation reference inspector without turning automatic choices into
   mandatory review.
5. Instrument the current planning/finalization paths and optimize only work that
   the new measurements prove is still duplicated.
6. Run platform, hardware and signing gates only when the required host or
   credentials are available.

## P0 - Play while offline audio is still preparing

- [ ] **Ready:** enforce one immutable source-audio authority predicate before
      publication, pre-generation routing, early prepared-WAV reservation and live
      playback. Only the current strict contract marker, verified media duration and
      checksum-bound semantic evidence may authorize `full`; remove policy switches
      that let callers bypass this predicate. Measure duration and establish
      full/partial correspondence once during game import or pre-generation, cache
      expensive semantic analysis by media SHA-256, and store the result in the story
      index and published pack. Duration alone is not semantic correspondence: reuse
      the existing local `Whisper tiny.en` normalized-transcript evidence, and treat
      inconclusive ASR as non-covering rather than inventing a duration threshold.
      Live reading must never scan, decode or hash game
      files: it performs only a bounded metadata lookup. Reject malformed records at
      the producer boundary. Downgrade an intact but inconclusive clip to
      unable-to-cover/`partial`, and continue story preparation with generated speech
      instead of rejecting the whole pack. A short game clip that does not match the
      full on-screen line may play only as a cue, followed by the complete prepared
      WAV or live TTS; base auto-advance on the complete VNTTS route. Find why
      affected records, including the Rhiannon `Stop it` case, arrive as `full` or
      bypass their checksum-bound semantic evidence. The captured Windows run proves
      the bypass: lines
      `314601:9` (`Stop it.`), `:13`, `:15`, `:22`, `:33`, `:36`, `:53` and `:62`
      all declare source audio but have `source_audio_completeness=unknown` and no
      duration; they were nevertheless routed as `game/exact` with
      `passthrough-unobserved`. Lines `:53` and `:62` then explicitly disabled
      auto-advance because completion timing was unavailable.
      Do not add legacy migration or a second compatibility predicate. If invalid
      data somehow reaches runtime, treat the source clip as unable to cover the
      line, start the prepared WAV or live TTS immediately, allow harmless overlap
      with the game clip, and base auto-advance only on the VNTTS playback outcome.
      Gate: every route consumer uses the same predicate, the live path performs no
      game-file I/O, one uncertain clip cannot invalidate the story pack, and
      invalid metadata cannot stall reading.
- [ ] **Investigate after session logging:** fix dialogue lines frequently playing
      twice. Correlate both playback
      attempts by session, occurrence and route identity; prevent source audio,
      prepared WAV and live TTS from claiming the same occurrence, including
      delayed completion and auto-advance races. Reproduce across a sustained
      chapter run and require zero duplicate starts, not one successful sample.
      The captured run contains eight confirmed duplicate generated-WAV
      starts for lines `314601:46`, `:50`, `:67`, `:68`, `:70`, `:71`, `:75` and
      `:76`, each with a new chunk ID 6.9-10.8 seconds later; the first route is
      commonly marked interrupted only after nearly a full playback. Do not dedupe
      by line ID or text because legitimate dialogue may repeat. Give sequence mode
      a session-bound event lease and non-sequence mode a session-bound routed-frame
      epoch/fingerprint; claim it while the occurrence is active and release it only
      before the first PCM is played. Gate: legitimate repeated lines still play,
      while one occurrence can produce at most one audible route.
- [ ] **Investigate first:** skip an ellipsis-only dialogue (`...`) without speech
      and without waiting
      indefinitely. Classify punctuation-only ellipses before audio routing,
      record an explicit silent outcome, and dispatch at most one advance through
      the existing focus/ownership guard only when auto-advance is enabled;
      keep lines containing spoken text plus an ellipsis on the normal speech path.
      The prepared index omits source sequence `314601:78`; the following captured
      eight-character no-match was sent to fresh MOSS, hit its frame limit and ran
      for 7.4 seconds instead of producing a silent outcome. Preserve a privacy-safe
      punctuation classification in diagnostics so the exact correlation is
      provable without storing dialogue text. Existing silent handling already
      exists in capture, sequence sealing and the incremental tracker; reproduce the
      failing route and identify which boundary drops the silent event before
      changing it. Gate: `...`, `…` and whitespace produce no MOSS request and one
      silent outcome; auto mode with valid ownership advances once, manual mode
      dispatches no key, and `Wait...` remains speech.
- [ ] **Investigate after session logging:** classify and eliminate avoidable
      `story-line-no-match` live fallbacks. The
      captured timeline contains ten fresh-MOSS no-match routes while the prepared
      story was active. Record the normalized-text hash and length, speaker match,
      cursor candidates and rejection reasons; use the prepared WAV whenever a
      candidate is valid, and reserve live TTS for a proven out-of-pack line. Do not
      loosen matching heuristics until every captured miss has a rejection reason.
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

## P0 - Let users inspect automatic voices before long pregeneration

- [ ] **Ready after extending the decision contract:** replace chance-like voice
      audition with a compact, non-blocking Voice plan inside the existing
      preparation flow. Automatic selection remains the default: generation must
      not require the user to approve every character. Show only characters in the
      selected stories, ordered by needs-attention first and then affected line
      count. Each row shows character, chosen voice source, production reference-set
      count and total duration, reason for the choice, affected line count and one
      of: automatic, explicitly approved, narrator, or needs attention. An automatic
      recommendation is not stored as a user decision; an explicit override is.
      Reuse the existing plan store, audition panel, portrait, asynchronous runner
      and checksum-bound preview cache, but first extend the data model: current
      decision persistence and UI filtering accept only `needs-audition` groups and
      cannot approve ordinary auto-selected `voice` groups.
      Inspector behavior:
      1. Selecting a row reveals the representative original reference plus a
         compact description of the exact ordered production set. Keep the full
         member list behind `Reference details`/`Show all`; do not force the user to
         audit dozens of clips. Permit individual reference/set replacement only if
         the production backend actually supports that change.
      2. Show source line, duration and `Play original`. Automatic ranking is only a
         recommended starting point, never a hidden random next choice.
      3. Show one fixed `Test selected reference` action. If an exact cached preview
         exists, replay it; otherwise generate with the production backend, model,
         profile, controls and reference set, cache it, then play it. Keep original
         and generated playback controls visually separate and label what is playing.
      4. Show the test text and an `Another phrase` action. Advance
         deterministically through suitable story phrases, preferring a longer or
         more expressive unused line after a neutral default. Keep one phrase across
         candidate references so comparisons remain meaningful.
      5. Offer `Use selected reference`, `Use narrator`, and `Choose another
         voice...`; the last action opens the existing Voices catalog/import flow.
         Do not add `Try another reference`, `None are acceptable`, separate
         ordinary/expressive modes or separate Generate/Play controls.
      6. Persist only explicit decisions against character identity, ordered
         reference checksums, backend, model, profile and synthesis controls. Reuse
         them across stories/restarts and invalidate them when one input changes.
      Only genuine ambiguity, no usable source, or a failed reference/preview is
      marked needs attention. Even then, the configured narrator is the automatic
      safe fallback, so unresolved review does not block generation. Existing
      decisions such as `Hotelier -> narrator` bypass attention. Gate: a new user can
      start automatically without reviewing every role, can inspect and override
      any consequential choice before generation, and sees the exact production
      reference set rather than a misleading single clip.

## P0 - Stabilize Windows audio and the OpenMOSS runtime

- [ ] **Investigate, then fix one device-quiescence boundary:** fix the Windows native audio
      teardown crash captured on 2026-09-14. The host
      `python.exe` failed with access violation `0xc0000005` in
      `msvcrt.dll+0x74452`; the dump places `wdmaud.drv` on the crashing stack and
      loads PortAudio. The last MOSS request completed successfully before the
      crash, after which a short playback was interrupted and live reading restarted
      in the same second. Both prepared-audio and live-TTS stop paths may call
      PortAudio from a non-owner thread. The caller may only request cancellation;
      the worker that owns the device performs stop/abort/close. First record
      owner/reason events and make `LiveDialogReader.wait` include all speech and
      playback futures. Then expose one bounded quiesce wait used before reader
      replacement across both backends. Do not paper over ownership with a shared
      lock that can deadlock against callbacks. Stress focus loss plus stop/restart
      at short-stream completion. Gate: no native
      crash, stale audio or overlapping output stream on Windows.
- [ ] **Ready:** preserve a sanitized summary of the previous completed or crashed
      session before the first new runtime event can overwrite it. Archive one
      bounded copy of runtime lifecycle, performance data, generation timelines and
      parsed managed OpenMOSS lifecycle fields; do not copy raw `server.log`,
      dialogue text or local paths into the support ZIP. Gate: a
      crash/restart fixture still identifies the prior live-reading and native-
      speech stages without dialogue text, screenshots, audio, paths or secrets.
- [ ] **Ready:** give every live-reader instance a session UUID and include it in
      generation-timeline identity. Integer generation numbers restart at one and
      currently merge separate readers into impossible timelines. Gate: two reader
      restarts with generation 1 remain two distinct timelines in diagnostics.
- [ ] **Ready, before the crash fix:** record the audio host API/device plus stream
      open, stop, abort and close owner/reason. Keep values bounded and privacy-safe.
      Gate: a support ZIP can correlate every terminal playback outcome with one
      stream lifecycle without recording audio or dialogue.
- [ ] **Ready, with Windows verification:** ensure a managed OpenMOSS server cannot
      survive an application crash. On Windows, launch it in an owned Job Object
      with kill-on-close semantics so normal exit, fatal Python/Qt failure and
      forced process termination close the whole process tree. Do not add next-start
      PID cleanup: PID reuse and the native server's lack of an authenticated
      ownership token make it unsafe. Never terminate an unrelated server.
      Add a subprocess crash test that proves no `moss-tts-server.exe` remains. In
      this incident server PID 11036 completed its final HTTP request normally while
      host PID 13844 crashed, so ordinary backend shutdown was never reached.
- [ ] **Validate on Windows:** run the qualified GPU/8 profile beside Reverse: 1999
      and confirm game-time VRAM headroom and responsiveness before making it the
      accelerated default. Retain CPU/4 as the fallback; extra CPU workers did not
      improve warm renders consistently on the qualified host. The captured GPU/4 run used 37 Vulkan
      layers but native telemetry reported no GPU-board samples, about 11.0 GB peak
      native RSS, about 2.1 GB host RSS and about 5.8 GB minimum free RAM; fix GPU
      telemetry and measure the real game-plus-render headroom rather than inferring
      acceleration from configuration.

## P1 - Reduce preparation and post-generation saving latency

- [ ] **Investigate first:** profile unchanged pre-generation planning before
      selecting an optimization.
      The captured run spent 23.65 seconds creating its voice plan and 20.16 seconds
      preparing generation input before rendering. Add phase timings around index
      reads, reference checks, hashing, copying and the existing identity lookup
      before introducing a cache. Then move the identity check ahead of expensive
      work and reuse unchanged story/voice/backend input without decoding or copying
      the same reference twice within one immutable planning transaction. Keep deep
      validation at trust and activation boundaries; file mtime/size is not proof of
      integrity. Gate: a cold and unchanged run produce identical plans, and
      telemetry proves which repeated work was actually removed.
- [ ] **Investigate first:** profile the complete path from the last generated line
      to the usable saved game pack on representative Windows and macOS stories.
      Record wall time, bytes read and hashed, WAV decode count, repeated manifest/state loads,
      copying, validation and activation so the UI names the actual current phase.
      The captured Windows baseline took 73.8 seconds after recovery: acceptance
      33.14 seconds, publication 38.09 seconds and activation 2.29 seconds. Three
      adjacent full validations each rescanned 1,071 files / 382.5 MB in about two
      seconds, so instrument the unaccounted acceptance/publication time separately
      from validation.
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
