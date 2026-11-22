# TODO

## Reader-first desktop experience

- [ ] Validate the [Reading journey](docs/reader-experience.md) with first-time
      and returning players. Ask them to choose a window, hear a line, switch
      games, stop, and find the page-turn setting without guidance.
- [ ] On macOS, qualify native output recovery after removing and reconnecting
      headphones during Reading, voice preview and CLI playback.

## Active - Support 31 Reading and recorder defects

Evidence: `vntts-support (31).zip` and diagnostic session
`1b42f6a6721a4848a9ee845c37837fd5.zip`, build `579f9a70`, 2026-11-17.

- [ ] Complete Windows process-loopback adverse-path qualification: run native
      clock and activation-blob lifetime tests, then check acquisition/writer
      failure and teardown; a rejected clock must retain previous/current packet
      values. Keep diagnostic source-correlation qualification separate above.
- [ ] Complete Windows video adverse-path qualification: check journal failure
      and failed finalization, retaining the original error and bounded teardown.
      Run native Windows recording regressions; ordinary readiness and normal
      stop no longer need to be repeated as unverified prerequisites.

## Active - Remaining CI qualification

- [ ] On Linux, qualify early ONNX telemetry suppression through ordinary
      startup and spawned OCR workers with a real model: inference and normal
      process exit must work without user flags.

- [ ] Qualify the refactored capture, cancellation and voice paths on hosted
      Windows/Linux, and Reading on hosted Linux after local gates pass.

## Native capture - remaining macOS qualification

- [ ] On suitable macOS hardware, check monitor/Retina changes, mixed DPI,
      multiple monitors, SDR/HDR and other fullscreen implementations. Check
      minimize/restore and explicit display capture independently; unsupported
      configurations must fail explicitly without a desktop fallback.
- [ ] If the ScreenCaptureKit application-connection interruption recurs, retain
      its NSError domain/code and verify the one-retry recovery against the same
      request deadline and fresh epoch. The original interruption is not yet
      reproduced reliably; automated recovery tests do not establish its cause.

## Active - Finish diagnostic-session native acceptance

- [ ] **D1 - Qualify remaining recorder adverse paths.**
      During a relevant native game run, change the actual default output device.
      For unexpected source exit, verify independent game/video and journals
      survive, then record the replacement worker under a new identity until the
      original deadline. Keep Windows native lifecycle acceptance on its own host.
      Qualify only captured intervals, including readiness/startup and final
      cutoffs; no physical-speaker claim.

## Rust audio native acceptance gaps

- [ ] Complete adverse-path qualification of `scripts/mac_live_recording/process_tap.m`:
      native permission denial, worker exit/restart, device change and reverse
      teardown. The positive-path native gate now has exact-source PCM correlation
      and full 300s/180s exact-window recordings; do not repeat it as an untested
      prerequisite. Keep explicit PID/path/start time/HAL ownership and no parent,
      desktop or global-audio fallback. Investigate ScreenCaptureKit -3805 lifetime
      interference only if serialized ownership reproduces it.

## Reading setup blockers found during persistent-OCR preflight

- [ ] Validate calibration visibility with the actual game. Exercise tray
      and wizard with a game parked outside the active screens, then show the
      same game and retry without reselecting it. Gate: reject partial/off-screen
      captures before preview, restore controls on failure and accept the fully
      visible window. Include movement/resize during capture, workspace changes,
      adjacent monitors, negative origins and display scaling; preserve the saved
      normalized dialogue region and cancellation behavior.

## Remaining OCR model selection and cleanup

- [ ] Choose multilingual model sets using labelled genuine game captures with
      Russian within English lines, French/German accents, partial text and moving
      names. Keep current configured languages until quality is qualified.
- [ ] Retire the CLI reference OCR backend and its helpers once fixed fixtures
      preserve the comparison coverage needed by the native backend.

## Remaining Reading and deferred layout work

- [ ] **Deferred document and new-game reading layouts.**
      Architecture: [text-reading-architecture.md](docs/text-reading-architecture.md).
      Plan these features separately when requested:
      - validate new-game discovery without catalog/resources, different text
        styling and short captions;
      - plan the shared layout-format migration for dialog_region/OCRRegions,
        story_region and future document areas before adding more readers;
        migrate persisted calibration/profile/preview/replay consumers together,
        retaining current ordinary calibration until that migration is complete;
      - extend the replacement-reading owner with per-document position, then
        implement the document reader, ordering and source-free overlap;
      - verify optional source/prepared-audio integration and strict sequence
        behavior across all implemented layouts;
      - verify new-game reading after those feature changes.
      The generic path must read valid OCR with a standard available TTS voice;
      no resource parser, story index or game-specific reference is required.
      Never inherit a previous game's source binding for a newly selected game.
      Register new readers through the common observation contract; do not add
      game/mode branches throughout controller, audio routing or navigation.
      Use the captured Easter backward-scroll pair to verify unchanged paragraph
      identity and clipped-prefix handling; capture new-text/page-turn sequences,
      clipped paragraph starts and full viewport jumps before accepting the flow.
      Verify source order, no rereading and preservation of queued speech.
      Use `.codex/investigations/text-location-20261110/` for the actual page and
      `.codex/investigations/text-location-20261109/` for normal/quote controls.
      Compare configured alternative reading areas with fallback-only text
      detection/line recognition, preserving the current normal-dialogue path.
      Reuse the offline detector
      evidence in `.codex/investigations/text-location-20261109/`.
      Preserve configured reading bounds. Generic screen reading may use optional
      source enrichment but must not require a catalog match. Explicit
      source-bound/sequence routes retain strict source authority.
      Book reading must preserve paragraph boundaries and already-read position;
      define page-advance behavior separately from per-dialogue auto-advance.
      Add explicit page presence/text-only semantics instead of the ordinary
      bright-dialogue gate; order detected lines geometrically before grouping.
      Preserve the location Narrator record: scrolling
      reveals it inside the right page as well as the left metadata. Exclude
      duplicate side-panel text by content bounds, not by deleting source rows.
      A backward viewport update must not reanchor automatic reading to an older
      source occurrence; keep ordinary dialogue navigation behavior unchanged.
      Agree further feature slices separately.

- [ ] **REC-02-01 - Native Windows recorder qualification.**
      On Windows10 2004/build19041+, record distinct selected-game and unrelated
      audio signals and prove only the selected process tree is present. Check
      speech-worker arming before PCM, late/retired workers, Stop/deadline,
      source/parent exit, device changes, partial CAF recovery, MP4 decode and
      truthful dropped-video metadata. Ordinary OCR capture must remain usable
      when optional Media Foundation components are absent. Native runtime and
      privacy acceptance are separate from the passing portable unit tests.

- [ ] **Audio query recovery (AUDIO-29-01):** if a native query fails again,
      retain its query/worker IDs and last parent/native phase to diagnose
      the failure before changing worker ownership or retry behavior.
- [ ] **OCR warning presentation:** low-confidence startup/live observations must
      preserve the last real speaker and dialogue in dashboard and Compact.
      Confidence/reason belongs to status; raw OCR must not enter status logs.

## Slow voice selection and story preparation

- [ ] Reduce the wait when reopening the voice plan. The November 22 Windows
      support export measured 14.7 seconds on both attempts. Building the
      voice inventory took 5.8–6.2 seconds and resolving the groups took
      3.8–3.9 seconds even when every group reused its library snapshot.
      Profile catalog construction and repeated candidate hash/duration work
      on the same selection, then remove the dominant redundant work. Compare
      reopen time and verify the same groups, references and saved choices.
- [ ] Reduce the wait for game voices in the voice editor. The same export
      measured 12.4 seconds to list characters and 10.3 seconds to prepare a
      selected character's references. Isolate index loading from candidate
      extraction and reuse unchanged results on reopening the same character.

## Active - Faster voice auditions and qualified reference selection

- [ ] **Validate general automatic reference suitability:** build reproducible positive/negative acoustic controls from authorized installed sources and qualify any new output rule against independently accepted examples; preserve variable repeat outcomes as uncertain. Store the fixture recipe and expected decisions in Git; private historical ratings are not a required input. Require evidence on known natural defect types, balanced shared texts/seeds and independent character/source groups before assigning reference-ranking weights; retain an uncertain outcome and verify sample-rate/gain sensitivity. Do not fit thresholds or invert failed metrics from these development labels; injected controls are not natural-defect precision. Further human listening must answer a concrete unresolved question using a few targeted cases and a stated stopping decision, not another exhaustive batch. Keep named references/ratings research-only and ordinary users free of per-character A/B review.
- [ ] **Finish reference-selection baseline:** compare the current trio with eligible outside references at the same three-generation budget using complete inventories regenerated from the same installed sources with recorded versions/checksums; keep Sonetto's routed-only counterfactual separate from successful imports. Test source/length diversity as a hypothesis against the current four-second ordering, with shared texts/seeds and all failures retained; do not infer synthesis quality from catalog size or equal technical scores. Create a small reproducible, human-accepted control set for false defect warnings, preserving ordinary intonation/stress variation; record source identity, text/seed and expected judgment in a tracked fixture recipe instead of requiring private WAVs. Qualify independent characters/performers and ownership of unlinked media separately from synthesis quality. Check held-out phrases; keep variable repeats, render-block dependence and complete-intersection exclusions explicit. Historical sweeps with different text/runtime are not before/after evidence, and any new listening must resolve a concrete uncertainty with a few targeted cases.
- [ ] **Calibrate useful synthesis-defect recommendations:** optimize assistance for the user's final choice, with visible uncertainty and tolerated errors, rather than requiring an infallible detector. Build tracked normalization/control fixtures that separate writing/boundary variations, ambiguous recognition and possible added/missing content; keep weak ASR evidence from imposing hard reference rejection. Verify practical usefulness independently before assigning calibrated confidence or reference-ranking weights; the user's illustrative 90% is not a measured result. Keep automatic replacement/retry qualification separate from advisory recommendations: preserve the original and establish net audible benefit before enabling replacement. Record cohort limits and do not fit exception lists or thresholds to those same examples. A historical single-seed reproduction found an ASR-exact seed+1 replacement; reproduce from recorded model/text/seed inputs before using that result, and require human acceptance independently of ASR. Padding changes conditioning content/length and is not a qualified preparation fix. Retain content, silence, clipping and perceptual checks as separate signals. Preserve ties and neither, count every attempted render, and do not infer absolute scores from comparative labels or globally blacklist a reference from one bad seed.
- [ ] **Qualify improved ranking:** separate character ownership from backend/conditioning-specific suitability; unsupported speech/music/noise/multiple-speaker/transcript signals remain unknown. Keep ASR and speaker similarity diagnostic until calibrated on independent anchors and held-out characters/performers; do not promote an arbitrary replacement for the four-second heuristic or require expensive qualification for every ordinary preparation.
- [ ] **Refresh older automatic suggestions when policy changes:** distinguish unreviewed legacy automatic bindings from explicit user choices and conditionally migrate only unchanged algorithm-created suggestions for the same character. Preserve concurrent user changes and immutable running/prepared job snapshots; current advisory recommendations intentionally retain existing bindings. Qualify migrations independently of fresh unbound suggestions.

## Repair current GitHub CI failures

- [ ] Qualify native UI test ownership so discarded tray widgets are destroyed on the GUI thread; obtain a reproducible owner/lifetime sequence before changing it. If native app exit0 after initial UI readiness recurs unexpectedly, capture the quit reason and in-flight task/drain order; manual closure versus unexpected exit remains unconfirmed. Keep that exit investigation separate from the reproduced retired-signal completion and from Rust feeding.

## Continue regular-file admission through remaining integrity owners

- [ ] On Windows, qualify candidate-directory replacement/alias refusal before cleanup, active claims and normal retry with native directory identities/locking/rmtree. Mac proofs do not establish Windows behavior.

- [ ] Qualify admitted SHA readers and direct/recursive/runtime copy behavior on Windows, including native metadata/alternate streams, DLL/junction handling and executable behavior. Local Mac output identities, metadata, relocated interpreter provenance and warm-cache throughput passed; cold-disk/network throughput and native model generation remain unmeasured.

- [ ] On Windows, validate admitted bounded WAV/PCM decoding, PNG/JPEG/TIFF screenshot decoding and RGB/crop after source closure, cancellable MOSS model hashing, optional reference duration and native log diagnostics. Exercise registered and legacy inline MOSS references, unchanged-reference cache reuse, cancellation/retry and decoder/source cleanup using real files; Mac descriptor and loopback tests do not certify Windows virtual I/O or actual model generation.

- [ ] On Windows, qualify complete and streaming admitted text reads: UTF-8/BOM/ASCII/default locale, regular aliases, source closure and JSONL first-line/iteration behavior. Verify the dependency-audit script starts before project installation. Mac FIFO/descriptor proof does not qualify Windows filesystem behavior or non-UTF-8 locale execution.

- [ ] On Windows, qualify admitted ZIP intake for MOSS runtime and managed game decoder, plus native-probe archive member sources. Verify valid/invalid archives, source closure, member bytes/metadata, refusal before publication and retry. Mac FIFO/byte-parity tests do not establish Windows filesystem or DLL behavior.

## Qualify remaining model dependency risks

- [ ] Qualify Coqui 0.28 with real pinned model weights and generated audio on
      CPU/CUDA. Check persisted voice reuse across upgrade;
      tiny GPT generation and constructor/config checks do not qualify audio
      quality, full checkpoint loading or accelerator behavior.
- [ ] Qualify the fixed candidate against MOSS Delay's trusted remote processor
      and model loading, including AutoModel's injected GenerationMixin and
      automatic custom-generation lookup. The captured default model snapshot
      has no custom_generate files; mutable/default or custom repositories are
      not a permanent reachability exemption. Preserve revision forwarding,
      CUDA/BF16 refusal, dtype/attention options and actual generated audio.

## Adopt Qwen for offline story preparation

- [ ] Repair Windows Qwen voice preview startup. The November 22 support
      export shows the managed runtime probe failing to load `libsndfile.dll`
      with error 0x7e. Check the runtime's packaged soundfile library and its
      dependencies in the worker environment, then start a preview there.

- [ ] Listen to transcriptless Qwen game-voice samples on Mac and Windows before
      choosing it as the default offline engine. The speaker-only conditioning
      path is functional but may preserve expression less well than a reference
      with verified spoken words.
- [ ] On the RTX 2070 Super 8 GB, generate the fixed character corpus with the Windows Qwen 0.6B runtime. Compare its voices with MOSS, confirm memory use and complete lines, and retain the audio and timings. The Mac Qwen 1.7B comparison has passed listening review but does not establish Windows 0.6B quality.
- [ ] Before making Qwen the preferred offline engine, review the pinned dependencies' CI exceptions for release and complete the Windows quality check. Then verify narrator setup and story preparation with Qwen and keep MOSS available.

## Active - Review remaining source-extraction UI

- [ ] In `reverse1999-extractor`, reduce empty Character Story table/portrait space so review and A/B actions are easier to reach.
- [ ] Re-render Character Story review, resolve Astra findings and risks, then run extractor UI tests and an independent code review. Integrate a released extractor revision into this project's pinned dependency only after the user authorizes the cross-repository workflow.

## Qualify the existing audio-bank index migration on macOS

- [ ] On the current Mac, reopen Stories after updating the app and confirm the
      stale English audio-bank index triggers visible automatic import, preserves
      prepared audio, and no longer reaches `Saved audio needs attention`. The
      importer and background UI route are covered by automated tests; do not
      overwrite the user's game-content cache merely for this validation.

## P0 - Play while offline audio is still preparing

- [ ] Explain or correct the support bundle's `live_sequence_mode=off` while
      `auto_advance_enabled=true` before attributing the missing plan to generation.
- [ ] Decide separately whether a sequence plan completed after audio publication
      should support a later safe successor publication.
- [ ] **Investigate from one fresh support archive:** `story-line-no-match` now
      records normalized OCR text/speaker hashes, lengths, eligible and
      speaker-matching candidate counts, missing identities, best bounded evidence
      and a concrete rejection reason without storing dialogue. Reproduce the ten
      prepared-story misses, classify each rejection, then fix only the proven
      boundary. Gate: every captured miss has a reason, valid candidates use the
      prepared WAV, and only proven out-of-pack lines reach live TTS.

## P0 - Stabilize Windows audio and the OpenMOSS runtime

- [ ] **Validate on Windows:** the captured `wdmaud.drv`/PortAudio teardown race
      now has owner-thread-only stream teardown, cancellable 50 ms PCM writes and a
      bounded reader quiescence barrier covering capture, OCR, synthesis and
      playback before restart or shutdown. Stress focus loss plus stop/restart at
      short-stream completion. Gate: no native crash, stale audio or overlapping
      output stream; the support archive records matching stream owner and lifecycle
      events.

- [ ] Investigate the two current authoring underflows from archive (20), at 21:23:54.642 (`VoiceAuditionPanel`) and 21:24:27.176 (`StoryVoicePreview`), using the player/attempt IDs and terminal outcomes in authoring telemetry. Both attempts reported failed playback rather than natural completion, and subsequent playback also completed; the archive does not establish whether physical output starvation, cancellation or another host condition caused them. Retain output-buffer/owner timing in a bounded reproduction before changing PCM buffering or completion rules.

## P1 - Qualify remaining Python and speech runtimes

- [ ] **Validate on a Windows CUDA host:** qualify the Python 3.14 MOSS Delay
      candidate with its atomic Torch/TorchAudio/TorchCodec stack and a supported
      shared FFmpeg build. Require CUDA import, one checksum-bound reference-conditioned render,
      finite audio and clean shutdown. CPU-only hosts
      must return a clear unsupported-backend result instead of loading the 8B
      model.
- [ ] **Ready after the production MOSS qualification:** make runtime preparation
      select only a qualified stack from detected hardware. Exercise absent runtime,
      interrupted download, retry and restart on clean macOS and Windows. Users
      must not choose Python or CUDA wheels manually.

## Deferred

- [ ] Prepare the application for distribution.

## Dialogue history on Windows

- [ ] Start a captured line with its assigned
      voice, select or filter to another line, then stop and confirm the audio
      and named status agree. Restore an unavailable engine and retry from the
      same window.

## P2 - Optional model experiments

- [ ] **Optional validation:** on a CUDA host, compare MOSS Delay 8B with MOSS Local
      4B on the existing checksum-bound 46-line corpus. Preserve group identities,
      WAV hashes, quality signals and model provenance; do not repeat the
      completed Local-4B/XTTS comparison.
- [ ] **Optional validation:** evaluate typed non-verbal events with official MOSS
      SoundEffect v2 on a CUDA BF16 host using the locked runtime, the pinned
      checkpoint, a small fixed corpus and multiple checksum-bound seeds.
      Require technical and blinded perceptual approval before adding a provider;
      unsupported effects remain explicit omissions.

## Investigate intermittent macOS Qt test crash

- [ ] Isolate the intermittent macOS qt-app SIGSEGV at `test_backend_setting_reloads_speech_without_app_restart` while `player_session._replace_runtime` runs in a pooled thread. The historical five-test prefix and full4488 run passed, but their private logs are not a reproducible input. Record the full current discovery order and native stack on the next failure, then narrow that failing prefix. Establish a repeatable owner/lifetime sequence and deterministic regression before changing runtime ownership; passing reruns do not establish a cause or repair.

- [ ] Reproduce/bisect the October 8 remainder SIGSEGV at `tests.test_history.DialogueHistoryDialogTest.test_slow_replay_keeps_qt_responsive_and_reports_completion` under a freshly recorded full-discovery order. The October 9 portrait selector (196modules) and an earlier3565-selected-test run reproduced the same slow-history-replay SIGSEGV, while identical selections also passed. Historical prefixes were not checked in: record the full current order and faulthandler/native output on recurrence, then bisect that captured prefix; causality is not established. Gate: narrowed repeatable Qt/pooled-thread owner sequence and a deterministic lifetime regression; a successful retry does not resolve it. The October 9 task/command full-run remainder twice crashed at tests.test_replay_shutdown.ReplayShutdownTest.test_failed_stop_retains_real_snapshots_until_workers_finish in Shiboken mainThreadDeletionHandler after all five Qt shards passed; record a fresh failing process order/native stack before selecting a repair. Unchanged main passed full3853; isolated replay, readiness/replay and preceding Qt prefixes passed. Readiness fixture ownership is now separately qualified on successful and failing assertions, and final local/fresh-main branch full3863 pass, but SIGSEGV causality remains unestablished. Narrow the owner sequence if it recurs; passing retries do not close this gate.

- [ ] Reproduce/bisect the October 7 qt-assets SIGSEGV at `test_model_verification_can_finish_after_close_without_updating_ui`: native stack enters QObjectWrapper disconnectNotify during QDialog child destruction. Compare the exact 21-test shard on main and the cancellation branch; four fresh main processes passed, so causality is not established. Record a fresh native stack and exact process test order on recurrence; gate: a narrowed repeatable teardown sequence and an explicit owner-lifetime check, without treating a successful retry as a native crash fix.

- [ ] Compare the October 6 qt-app-2 native GC SIGSEGV in `test_settings_and_assets_apply_without_blocking_qt_events` with the existing owner-deletion investigation. Stack enters QAction disconnectNotify/QMenu destruction while player-session configuration runs. The exact original 43-test prefix passed in three fresh processes and under GC thresholds (50, 5, 5); the confirmed menu/action native ownership repair passes its separate lifetime regression, but is not a proven crash fix. Next gate: reproduce/bisect the preceding-owner sequence with faulthandler before further lifecycle changes.

- [ ] A macOS changed-test run intermittently terminated with `shiboken6` `mainThreadDeletionHandler` SIGSEGV during pure-Python `test_player_session` after UI tests. The exact final 10-test sequence passed 10 times, the full onboarding/person/player sequence passed five times, and a later 2734-test selector passed. Reproduce and bisect an earlier Qt owner in the remainder shard with faulthandler before changing cleanup; record the full failing order and native stack if it recurs, then add the minimized reproduction to tracked tests. Gate: a narrowed failing sequence and a deterministic cleanup check.

## Investigate calibration OCR test startup after full discovery

- [ ] Reproduce/bisect the full remainder prefix preceding `tests.test_calibration.DialogRegionOverlayTest.test_cancelled_review_releases_stalled_ocr_subprocess`: one October 6 full run timed out before the fixture start marker, while the isolated case and all17 calibration tests passed. Record the current full-discovery order and capture worker/Qt/subprocess state before changing timeout or cancellation; the historical private log is not a prerequisite.

## Active - Continue cleanup ownership expansion

- [ ] **Windows job acquisition next gate:** qualify actual native failures before assignment, before resume and after resume on Windows. Current `__init__` closes job through raw cleanup and allocates `_ExtendedLimits` after handle acquisition outside its guard; assign/resume finally blocks can replace primary errors. Stage the existing cleanup leaf only after native ownership checks; prepare limits before acquisition. Four CloseHandle results are ignored and close clears `_handle` on FALSE. Microsoft documents FALSE as failure, but it can mean INVALID_HANDLE rather than a leaked valid handle; preserve returned status/last error and prove valid-handle cleanup and no runnable child with real Windows failure injection. Keep the two-second process wait budget; successful host-crash test and mock cleanup order do not qualify these failures.

## Remaining - Shared audio storage

- [ ] Qualify shared WAV hard links on the supported Windows filesystem.
      Keep explicit same-filesystem/link errors and existing audio intact; do
      not add a silent copy fallback.

## Deferred - Optional status-message design review

- [ ] Consider replacing the global last-message strip with context-appropriate
      feedback: reading state beside reading controls, generation progress in
      preparation, local errors beside the affected action, and technical events
      in diagnostics. Reserve a top banner for important application-wide problems
      that remain relevant until resolved; ordinary updates must not overwrite
      them. Avoid confirmations when the interface already makes the result clear.
      This is an optional design idea, outside the chapter-run defect queue and
      acceptance gates. Agree a concrete scope before implementation.
