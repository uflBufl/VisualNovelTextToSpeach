# TODO

## Reader-first desktop experience

- [ ] Validate the [Reading journey](docs/reader-experience.md) with first-time
      and returning players. Ask them to choose a window, hear a line, switch
      games, stop, and find the page-turn setting without guidance.
- [ ] Check keyboard access to the game window picker on Windows.
- [ ] On macOS, exercise game audio and focus changes with a prepared chapter.
      Verify keyboard access to the window picker and Stop.
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

## Active - Support 30 Windows Reading repairs

Evidence: `vntts-support (30).zip` and `reading-diagnostics.7z`, build
`e7bf2665`, 2026-11-17.

- [ ] **Pre-OCR latency qualification:** on Windows, correlate capture, layout
      detection, queueing, OCR and canonical admission separately under actual
      background generation. Recheck the same scene with static and changing
      text; exact-repeat reuse does not remove inference for new pixels. The
      archived frame age before OCR was190ms vs55ms, while raw OCR medians were
      129ms vs124ms in different scenes, so those are not a controlled benchmark.
      The layout span still excludes model preparation and is not retained
      separately in the timeline. Preserve Story coverage and current-frame,
      confidence and ownership gates while attributing the remaining delay.
- [ ] On Windows, check screen feedback on uncertain or empty observations and
      copy the recognized fragment during background generation. After Stop and
      restart, confirm a previous fragment is not shown for the new dialogue.
- [ ] Verify that changing generation priority preserves completed audio.

## Active - Remaining Windows Reading qualification

- [ ] After an unavailable optional announcement, change to a working narrator
      or engine and verify retry. Suppressing repeated failures is not external
      Pocket cloning authorization.

## Active - Remaining dialogue recognition and original-speech qualification

- [ ] Verify genuinely partial originals separately in anecdotes/character
      stories, which retain ASR.

## Active - Windows story preparation ASR compatibility

- [ ] On Windows, resume the affected new-story preparation and verify source
      audio analysis, voice planning and synthesis complete. Verify live progress
      and cancellation through stdout pipes for duration, semantics, references
      and portraits. Local Whisper inference and pipe regressions do not replace
      this platform gate.
      During automatic reference qualification, verify model startup, preview
      rendering and ASR show the current character/reference counters and elapsed
      time in the main panel, including cancel/retry and a small Windows window.

## Active - Remaining SQLite runtime generation qualification

- [ ] On Windows, verify fallback, review and newly committed audio becoming
      available to an already active reader.
      Confirm publication does not replace an open database or require working
      JSON.

## Active - Remaining CI qualification

- [ ] On Windows and Linux, qualify early ONNX telemetry suppression through
      ordinary startup and spawned OCR workers with a real model: inference and
      normal process exit must work without user flags. The fresh-process/spawn
      regressions and macOS native probes do not replace these platform gates;
      ORT1.29's Windows telemetry provider differs from the POSIX provider.

- [ ] Qualify the refactored capture, cancellation, voice and reading paths on
      hosted Windows/Linux and GPU runtimes after local gates pass.
      Verify playback buttons distinguish preparation from actual device onset,
      including cancellation during synthesis and rejected playback ownership.

## Native capture - remaining macOS qualification

- [ ] On suitable macOS hardware, check monitor/Retina changes, mixed DPI,
      multiple monitors, SDR/HDR and other fullscreen implementations. Check
      minimize/restore and explicit display capture independently; unsupported
      configurations must fail explicitly without a desktop fallback.
- [ ] If the ScreenCaptureKit application-connection interruption recurs, retain
      its NSError domain/code and verify the one-retry recovery against the same
      request deadline and fresh epoch. The original interruption is not yet
      reproduced reliably; automated recovery tests do not establish its cause.

## Active - Native chapter UX and prepared-voice trial (2026-10-25)

- [ ] **UX-25-02: Stories scrolling.** Check the story list and details pane
      with a physical mouse wheel or trackpad.

## Active - Windows story import and persistence

- [ ] Verify the affected Windows installation after the user's manual cleanup and regeneration. Activate a prepared story, save ordinary preferences, remove prepared audio and restart: preferences/profiles must remain intact, source stories must remain available, and missing prepared playback must be logged without breaking startup.

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
      interference only if serialized ownership reproduces it. See R12 for the
      remaining source/output discontinuities; positive capture is not gap removal.

## Reading setup blockers found during persistent-OCR preflight

- [ ] Validate calibration visibility with the actual game. Exercise tray
      and wizard with a game parked outside the active screens, then show the
      same game and retry without reselecting it. Gate: reject partial/off-screen
      captures before preview, restore controls on failure and accept the fully
      visible window. Include movement/resize during capture, workspace changes,
      adjacent monitors, negative origins and display scaling; preserve the saved
      normalized dialogue region and cancellation behavior.

## Persistent OCR remaining qualification

- [ ] Separately verify native library/model loading on Windows; offline process parity
      and mocked DLL layouts do not qualify native loading.
- [ ] Choose multilingual model sets using labelled genuine game captures with
      Russian within English lines, French/German accents, partial text and moving
      names. Keep current configured languages until quality is qualified.
- [ ] Retain the command-line OCR path only as an explicit benchmark/parity
      reference while native platform qualification is unfinished. After native
      Windows parity passes, remove the CLI reference backend/helpers and
      replace their comparison tests with the qualified fixed fixtures.

## Remaining Reading and deferred layout work

- [ ] **RUN-17-01 - Qualify catalog rebuild progress and Windows indexed search.**
      During the next actual content update, verify visible progress for rebuilding
      an already imported catalog on its existing background path. Initial index
      construction belongs to admission/update and must not repeat on Read.
      On Windows10, exercise FTS5/RapidFuzz and indexed query/cancellation;
      shared Python logic and dependency collection are not native acceptance.

- [ ] **REC-12-01 - Qualify recording resize on Windows10.** On its host, record
      shrink/grow, odd sizes, aspect changes and fullscreen/window transitions.
      Decode the fixed-resolution MP4: require the entire selected window, fresh
      black borders and no desktop/covering-window pixels. Verify timestamps,
      Stop and cleanup. Cross-compilation and pixel tests do not qualify native
      WGC/Media Foundation behavior.

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
- [ ] **OCR-03-01 - Remaining native matching checks.** Verify punctuation-split names and final-letter OCR errors choose ready
      generated audio without transient live synthesis or repeated playback. Verify
      typewriter prefixes still wait for full text except the deliberately owned
      early-prefix route; full recognition must confirm that route once. Noisy
      speaker/partial observations before and after full confirmation must retain
      the early canonical identity without replay, including after an idle
      interval; a genuinely different canonical line must still play. A crop
      containing an extra number/negation must remain unresolved rather than discard
      that content as decoration. Keep raw observations and prepared route identity
      when diagnosing a miss.

- [ ] Complete only unexercised controls: failed/not-ready policy and
      manual/other punctuation. Prepared Pause/Resume,
      fullscreen/window continuation and volume HUD have been exercised. Keep
      controls separate from autonomous chapter metrics; diagnostic video's bounds
      guard still intentionally ends a recording when fullscreen changes its size.

Other remaining defects and native checks:

- [ ] **OCR-01-01 remaining native layout edges.** Verify legitimate single-letter
      replies, punctuation, absent names and early prefixes under retained layout,
      and calibration invalidation when a game genuinely changes UI style.
      Keep dividerless startup distinct from a previously established divider.
      Preserve original lossless evidence if another background artifact passes;
      do not add minimum-length/dictionary filters or timeout skips.

- [ ] **REC-02-01 - Native Windows recorder qualification.**
      On Windows10 2004/build19041+, record distinct selected-game and unrelated
      audio signals and prove only the selected process tree is present. Check
      speech-worker arming before PCM, late/retired workers, Stop/deadline,
      source/parent exit, device changes, partial CAF recovery, MP4 decode and
      truthful dropped-video metadata. Ordinary OCR capture must remain usable
      when optional Media Foundation components are absent. Native runtime and
      privacy acceptance are separate from the passing portable unit tests.

- [ ] **OCR-29-01 remaining native edges:** verify genuine typewriter-prefix and ambiguous
      same-text speaker checks. Neither may gain full-line authority from an
      unreadable name. Preserve native crop, speaker/dialogue confidence and
      actual playback evidence for these unexercised paths.

1. **Other deferred native checks.**
   Preserve the separate manual-mode and generation-failure cases.
   - **Audio query recovery (AUDIO-29-01):** if a native query fails again,
     retain its query/worker IDs and last parent/native phase. Verify that
     failed playback does not advance the game and that ordinary Stop/Start
     replaces the failed worker and plays the prepared phrase once.
   - For punctuation-only dialogue encountered in game, preserve the shown
     signs and speaker, suppress duplicate transitions and symbol-only TTS, and
     retain original/prepared audio and approved spoken projections. Unreadable
     OCR must keep waiting, including at different native display scales.
   - **Prepared matching remaining edges:** after the approved3-character
     edit threshold and Latin-diacritic folding, reject ambiguous/incomplete
     matches and preserve protected numbers/negation. Use neighbouring wrong-line
     examples if a mismatch remains; do not relax further without evidence.
   - **Prepared completion remaining edges (RUN-25-08):** check manual game
     advance lets a started WAV finish, while pre-dispatch scene changes and
     explicit application cancellation stop stale work. Include speaker
     announcements, original-audio lead and pending preparation; late completion
     must not finish the next occurrence's cursor.
   - **Generation budget:** exercise a synthesis limit with normal automatic
     retries and retain the actual limit and outcome.
     Require complete speech or a clear terminal failure, no truncated WAV in
     the ready pack, responsive cancellation and preserved ready work on resume.
   - **OCR warning presentation:** low-confidence startup/live observations must
     preserve the last real speaker and dialogue in dashboard and Compact.
     Confidence/reason belongs to status; raw OCR must not enter status logs.
   - **Readiness after reload (RUN-25-01):** validate settings and prepared-pack
     activation, profile changes, cancellation and restored-runtime rollback.
     Checks must wait for the current operation, discard obsolete results and
     probe the effective configuration once; no false OCR installation error.

## Investigate reported audio interruptions

- [ ] **R12 - Investigate if audible interruptions recur.**
      If reported again, match the affected line to its source WAV and recording
      to distinguish playback loss from a capture issue.
      Compare historical Python evidence only with matched source/device/rate;
      no GIL-only attribution, launcher monkeypatch, WAV editing or buffer inflation.
      Disambiguate repeated identical WAV occurrences before treating an NCC peak
      as an authoritative onset; a matching source alone does not identify playback.

## Deferred - Windows support archive (20): defects and diagnostic gaps

Evidence summary: Windows support archive (20), exported 2026-10-21 22:07 UTC, Windows build `4bad9c73`; code paths rechecked on `1f4caa46`. Times below are archive UTC. Keep the already-fixed Settings/selection/original-playback delays separate from these remaining items.

- [ ] **S20.4 - Investigate on Windows: account for remaining voice-plan wall time before choosing a broader fix.** The historical `_create_voice_plan` took 408.915 s: candidate preparation 40.576 s and `voice-plan-store` 368.334 s; about 308.962 s of the latter was not attributed by the retained spans. Capture fresh cold and warm runs with the actual Windows/model workload using correlated `operation_id` timings for settings context, grouping, routing, reference qualification, applying choices, persistence, audition cache admission, queue wait, model startup, render, preview quality and ASR advice. Compare disjoint phases with the inclusive total; do not add inclusive parent and child durations. Inspect cache hit/miss, registry-build, reference-read and reference-preflight counters, and retain any unexplained gap explicitly. Identify the measured dominant work before choosing further reuse/deferment; preserve quality, admitted bytes, explicit choices and responsiveness. Fixture improvements do not diagnose the historical 309 s or qualify deferred Windows acceptance, large-pack finalization or GPU headroom.

## Qualify Settings on native Windows and macOS

- [ ] Confirm the Settings opening fix on the reported Windows setup: archive (20) measured two pre-fix Settings pack checks at 3.487 s and 3.120 s, each scanning 1,513 files. After `1f4caa46`, measure click-to-first-frame while idle and while Reading is active; distinguish initial rendering, intentional speech draining and Save preflight. Preserve Save errors and Cancel behavior.

- [ ] With verified background workspace isolation or an explicitly approved native session, edit Settings across sections, open the file and engine selectors, save and cancel nested voice choices, and confirm keyboard focus. On Windows, verify native shortcut symbols and that shortcut changes take effect after Save. Reset a saved non-100% speed on Windows and cancel to verify the original speed remains. Then reset again, save, reopen Settings, and play an existing prepared line at 100%. Retain native focus, popup placement and audible playback evidence. Offscreen Qt checks cover persistence, rollback, literal text, navigation and layout, not OS integration or physical output.

## Qualify Reading setup with native capture and audio

- [ ] With verified background workspace isolation or an explicitly approved native session, run Reading setup on macOS and Windows: choose a real game, recover from a missing audio device, calibrate, test/cancel/retry, change the region after success, then save. Confirm actual audio stops, a new area requires a new successful test, cancellation restores saved runtime settings, and Save leaves Reading visible without starting playback. Check full errors and keyboard focus; retain focus, audio and timing evidence. Offscreen tests cover transitions, persistence, clipboard and delayed window handoff, not physical capture/audio.

## Qualify compact controls over a real game

- [ ] With verified background workspace isolation, or an explicitly approved native session, exercise compact controls on macOS and Windows over a fullscreen game: Start, Stop, Cancel start, Read once, pause/focus loss and expected-line recovery. Confirm capture/audio stop, no focus theft, screen-edge placement, and keyboard access to the complete last speaker, latest message and unavailable reason. Retain native focus, screenshot and audible-stop evidence. Offscreen tests qualify state, dispatch, layout, scrolling/copy and focus indicators, not fullscreen behavior.

## Qualify native local voice import

- [ ] With verified background workspace isolation, or an explicitly approved native session, open the audio file picker by keyboard on macOS and Windows; choose and cancel multiple local recordings, then retry after a file becomes unavailable. Verify focus restoration and the readability of invalid-name errors and replacement prompts. Enter/Escape must keep the old voice; explicit Replace voice must update only the intended custom character. Use a disposable library and retain native focus evidence. Offscreen Qt tests and renders qualify draft, keyboard dispatch, scrolling and guarded publication, not native picker placement.

## Qualify native macOS permission recovery

- [ ] In an explicitly approved native macOS session (OS prompts and System Settings may interrupt the desktop), test Screen Recording and Accessibility separately: Request, deny/grant/retry, and Open/Manage Settings. Confirm each opens the corresponding system control; change access and return to VNTTS, then verify automatic status refresh and manual Refresh status. Check pending-request Close/Escape, keyboard focus; preserve screenshots and focus evidence. Offscreen tests verify dispatch, state, keyboard, scroll/copy and exact target URLs, not native OS routing or prompts.

## Qualify native OCR review and editor accessibility

- [ ] Once native UI can be launched without interrupting the desktop, check saved OCR corrections on macOS and Windows: put the narrow editor near screen edges, hover both long source/replacement cells and inspect each with F2/Escape. Confirm full values remain readable, hover stays usable, native tooltips remain on screen, and exiting inspection leaves Save disabled. Retain full-window/tooltip captures and keyboard evidence. Offscreen tests and cropped tooltips verify content, not native placement or focus.

- [ ] With verified background workspace isolation or an explicitly approved native session, review a captured uncertain line on macOS and Windows. Check Tab/arrow access to every footer action, literal-text copying and tooltips near screen edges. Save a scoped correction, reopen after an external rule edit, then Read Once on a real game and confirm the updated correction is used. Retain native focus and capture evidence; offscreen tests do not qualify OS routing.

## Investigate reported slow-sounding voice playback

- [ ] Identify and retain an actually slowed audible sample from the original/preview/reading paths, then compare its native DAC duration with its PCM frame count and rate before changing playback or synthesis speed. The current Mac reproduction plays Centurion original 12.51 s and MOSS preview 3.84 s at matching DAC durations, with 100% configured speed and no underflow; this does not explain the user's earlier report.

## Active - Faster voice auditions and qualified reference selection

- [ ] **Validate general automatic reference suitability:** build reproducible positive/negative acoustic controls from authorized installed sources and qualify any new output rule against independently accepted examples; preserve variable repeat outcomes as uncertain. Store the fixture recipe and expected decisions in Git; private historical ratings are not a required input. Require evidence on known natural defect types, balanced shared texts/seeds and independent character/source groups before assigning reference-ranking weights; retain an uncertain outcome and verify sample-rate/gain sensitivity. Do not fit thresholds or invert failed metrics from these development labels; injected controls are not natural-defect precision. Further human listening must answer a concrete unresolved question using a few targeted cases and a stated stopping decision, not another exhaustive batch. Keep named references/ratings research-only and ordinary users free of per-character A/B review.
- [ ] **Finish hardware qualification of audition latency/resources:** measure physical audible start and peak/model memory; qualify rapid navigation, close/cancel, explicit unload/reload and switching distinct model weights on Windows/native MOSS and additional supported backends. Record a fresh reproducible Mac/Windows baseline with the chosen model and hardware; historical Mac experiments do not establish hardware-wide certification. The Mac replacement check used two identifiers for identical weights; native model/language restoration and canceled replacement retry still need real hardware checks. Qualify the 300 ms first-ready target across devices, separating remaining native output initialization from cache lookup; retain device-change/error/cleanup checks before considering stream warmup. Do not broaden the speculative window beyond three requests.
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

## Validate OpenMOSS progress on Windows

- [ ] The September 29 22:48 support bundle still records `[WinError 5] Access is
      denied` replacing `generation-state.json` during another 568-line job.
      Diagnose the writer/reader or antivirus sharing boundary, then confirm
      sustained progress updates, cancellation and Continue retain completed
      lines without frequent state reads or another replacement failure.

## Adopt Qwen for offline story preparation

- [ ] Listen to transcriptless Qwen game-voice samples on Mac and Windows before
      choosing it as the default offline engine. The speaker-only conditioning
      path is functional but may preserve expression less well than a reference
      with verified spoken words.
- [ ] On the RTX 2070 Super 8 GB, generate the fixed character corpus with the Windows Qwen 0.6B runtime. Compare its voices with MOSS, confirm memory use and complete lines, and retain the audio and timings. The Mac Qwen 1.7B comparison has passed listening review but does not establish Windows 0.6B quality.
- [ ] Before making Qwen the preferred offline engine, review the pinned dependencies' CI exceptions for release and complete the Windows quality check. Then verify narrator setup and story preparation with Qwen and keep MOSS available.

## Active - Finish repeated UI scanability rollout

- [ ] Validate the integrated voice-route layout on Windows: wheel and keyboard navigation must reach the last route and Inspect/Edit/Generate actions; clicking or double-clicking a row must select and inspect that character. Check all-or-none portraits when game references are mixed, and retain screenshots.

## Active - Review remaining source-extraction UI

- [ ] In `reverse1999-extractor`, reduce empty Character Story table/portrait space so review and A/B actions are easier to reach.
- [ ] Re-render Character Story review, resolve Astra findings and risks, then run extractor UI tests and an independent code review. Integrate a released extractor revision into this project's pinned dependency only after the user authorizes the cross-repository workflow.

## Validate game voice provenance on Windows

- [ ] In Stories, choose a different reference through `Use this voice` and confirm the voice plan returns promptly without checking all saved audio. Then start generation and confirm the deferred audio check succeeds; attach a support archive if either step still takes over a second before generation starts.
- [ ] On Windows, confirm a character receives a game voice automatically when equally ranked story recordings are available, and genuinely unresolved roles show the review action prominently before generation.
- [ ] Validate the repaired mid-preparation game-index refresh on Windows without deleting the import or choosing Centurion again. After voice discovery updates `story-index.jsonl`, confirm Step 2 resumes with the same story selection and saved narrator. If a selected story disappears, confirm the saved voice remains and Stories asks for a new selection; Cancel during refresh must close the dialog.
- [ ] Open Voice plan and Voices with a fresh installed-game import. Confirm known game dialogue displays its original text, unlinked bank clips are explicitly labelled and available only for manual choice, and a manually selected reference stays selected after reopening. Old candidate catalogs should refresh without deleting the game import.
- [ ] On Windows, refresh an existing Centurion candidate catalog from the installed game. Confirm Voices and Story inspection show all usable character references (not just the three automatic recommendations), each original transcript contains spoken words once without labels such as `Greeting` or `Night`, and distinct in-game line IDs still resolve to the shared audio. No game-import deletion should be needed.

## P0 - Validate automatic game-update refresh on Windows

- [ ] On the current Mac, reopen Stories after updating the app and confirm the
      stale English audio-bank index triggers visible automatic import, preserves
      prepared audio, and no longer reaches `Saved audio needs attention`. The
      importer and background UI route are covered by automated tests; do not
      overwrite the user's game-content cache merely for this validation.
- [ ] After a real Reverse: 1999 update, open Stories without using Add or import
      content. Confirm that the update is detected, the cancellable import has a
      visible status, new stories appear in the existing Game content source,
      previously selected stories and voice choices remain, and an unchanged
      second opening starts no import. If the import fails, confirm the old story
      catalog remains usable and the import can be retried. Check that obsolete
      story-candidate caches are reclaimed without touching saved jobs or narrator
      references.

## P0 - Play while offline audio is still preparing

- [ ] **Validate mixed-stage semantic classification on Windows:** September 29
      support archive (17) selected five stages, four previously prepared;
      Step 2 measured 90/90 source voices in 25 seconds, then the extractor
      ASR publisher failed twice with `Timed source cue identity is invalid`
      (18 and 17 seconds). The fixed extractor must leave untimed/absent
      `unknown` cues untouched and reach voice choice on the same five stages.
      Check that a retry reuses measured durations, preserves prepared audio,
      and does not rerun semantic classification after success.
- [ ] **Validate remaining Stage 1 OCR routes on Windows:** `Hey!` acquired
      trailing OCR noise in the September 29 screenshots. The support bundle was
      captured before those screenshots, so its 21 `story-line-no-match` routes
      and seven unconfirmed key dispatches cannot identify each pictured
      occurrence. If wrong reading, live fallback, failed skip, or manual
      recovery remains, collect a same-run support archive and screenshots to
      classify it against exact stage/line IDs.
- [ ] **Validate installed-game provenance on Windows:** the September 29 job
      was tagged `selected-story-index` even though its path is the installed
      Reverse: 1999 catalog. Discovery and resumed-job fixes must preserve the
      canonical `reverse1999` provider without relabelling arbitrary indexes.
      Confirm reopening the existing job enables selected-source ASR
      classification and prepares a sequence plan before stage playback.
- [ ] **Complete remaining native acceptance after OCR-cancellation repair.**
      Repeat prepared reading with background MOSS and selected-window/process-audio
      recording. Verify genuine incomplete phrases wait and admitted phrases complete
      without unsolicited interruption. Inspect `observation-invalidated` and
      `queue-cancelled` with confidence/threshold and playback ownership to identify
      any remaining interruption; uncertain OCR must withhold auto-advance without
      suppressing admitted speech. Run from a cold worker after the capture-arming repair
      so the first clip is qualified too, and verify a replacement worker arms its
      own process tap before PCM. Explicit recording failure must remain visible
      and mark the measurement incomplete while allowing Reading to continue.
      Do not qualify the whole run from only
      successful clips; keep interrupted and missing-source attempts separate.
- [ ] **Validate other short and known-speaker OCR in Stage 1:** test very short
      utterances (`hmm`/interjections), a known speaker such as Ever-Echo, and
      legitimate numeric names. The nameplate must stay separate from dialogue
      and known story lines must resolve before live synthesis. Inspect unstable
      nameplate pixels if the visual-successor gate still resets to one candidate
      frame.
- [ ] **Validate matching game speech on Windows:** offline preparation now
      measures selected installed clips and classifies exact timed cues with a
      pinned local ASR model before generating; untimed or partial cues remain
      TTS fallbacks. Confirm one exact ASR match uses game audio without
      synthetic overlap and one partial cue still uses TTS. The reported line
      is indexed as `reverse1999:314501:92`, media `703844389`, text `We should
      all stay on our own "island."`, but its source-audio completeness and
      duration are unknown; the September 29 runtime routed it to generated
      speech with `source-audio-authority-unavailable`. Confirm that the fixed
      game provenance leads to measured/ASR-classified audio for this line
      before claiming its double reading is fixed.
      Check that cancelling the one-time model
      download takes effect after its current file and never blocks a retry.
- [ ] **Deferred Windows qualification - sequence-led prepared playback:** safe linear stages
      now acquire a checksum-bound raw-step plan in the background; runtime and
      published packs use audio-auto only with the plan and enabled auto-advance.
      Branches, gaps, missing bundles and incomplete cumulative coverage fall
      back to OCR. In stage `314501`, confirm the 107-step plan is acquired,
      and explain or correct the fresh support bundle's `live_sequence_mode=off`
      while `auto_advance_enabled=true` before interpreting a missing plan as
      a generation failure. Confirm known WAVs start promptly, no wrong
      speaker/duplicate/unsolicited key occurs, and manual resync still works.
      Compare capture/OCR/first-PCM timings with
      the September 29 baseline before changing capture cadence. If plan creation
      finishes after a very short generation and publication, the pack currently
      remains in OCR mode; decide whether a later safe successor publication is
      worth the added complexity only after measuring this case.
- [ ] **Validate stage preparation on Windows:** multi-stage collections now
      expose independently selectable source stage IDs grouped under their
      parent story, with per-stage readiness, and publishing Stage 2 retains
      Stage 1 audio. Confirm the affected eight-stage story shows one story
      with eight selectable stages; parent checkboxes, filters, expansion and
      readiness work. Selecting only an unread stage
      must not regenerate finished stages, and Stage 1 remains
      playable after a later stage is prepared. If the source reorders a stage
      or changes its checksum, verify the saved job is rejected explicitly.
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
- [ ] On Windows, resume preparation after a short phrase was rejected for
      trailing silence. Check that retry can recover the phrase and that clips
      which exhaust the synthesis duration limit fall back to live speech
      without aborting publication of the remaining recordings.

## P0 - Validate the non-blocking Voice plan

- [ ] **Validate changed-story edge cases on Windows:** if a previously selected
      story disappears after a game update, the saved Centurion choice must remain
      while Stories requests a new selection. Cancel during content refresh must
      close the dialog. The normal same-story refresh already passed player review.
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
      production preview; voice-reference and preview-phrase selectors can move
      backward and forward independently; returning to a previously generated pair
      reuses only that pair's preview; `Use <voice>`, narrator fallback, automatic
      choice and `Change selected voice...` are clear; and an explicit choice
      survives reopening while an untouched recommendation remains automatic.
- [ ] **Validate the no-choice voice route on Windows:** in a real story, return
      from Inspect selected voice before playback, during a preview and after a
      failed save. Confirm Back becomes enabled, no reference is silently saved,
      and the player can pick narrator fallback or return later.

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

- [ ] Investigate the two current authoring underflows from archive (20), at 21:23:54.642 (`VoiceAuditionPanel`) and 21:24:27.176 (`StoryVoicePreview`), using the player/attempt IDs and terminal outcomes in authoring telemetry. Both attempts reported failed playback rather than natural completion, and subsequent playback also completed; the archive does not establish whether physical output starvation, cancellation or another host condition caused them. Retain output-buffer/owner timing in a bounded reproduction before changing PCM buffering or completion rules.

## P1 - Measure remaining player latency

- [ ] On Windows, prepare stages A and then A+B from the same installed game.
      Check the duration and ASR cache hit/miss counts in the support log and
      confirm only B is newly analyzed; compare the second run with the earlier
      90-cue/25-second run. Cancel and retry once to confirm incomplete private
      output is not reused as published evidence.
- [ ] **Validate repeated Voice plan in the real UI after S20.2-S20.4:** on the local 253 MB reference
      index, repeated role selection fell from 9.18 to 0.37 seconds. The plan-store
      fixture now produces identical groups while reducing library reads from
      3646 to 214; warm creation took about 0.25 seconds. On an installed game,
      a Windows run still took roughly 30 seconds to scan voices for four selected
      stages. Capture a fresh support archive and compare first open with reopen:
      split semantic audio classification, extractor candidate preparation, and
      plan-store inventory/routing timings; require the phases to explain the
      whole wait and saved choices to remain identical. Prior Windows archives
      show the cold extractor decoding hundreds of references while a cache hit
      takes under a second; changing the selected role set creates a new batch
      cache identity. If extraction dominates, reuse checksum-bound per-media
      decoded/quality evidence across overlapping stage selections or defer
      decoding non-previewed references until inspection, without hiding the
      complete usable reference list or weakening voice-quality ranking. If
      routing dominates, use the per-candidate library/manifest counters in S20.2-S20.4
      to isolate its remaining cost. Verify both cold and warm timings and identical voice choices.
- [ ] **Validate combined PSM 6 OCR on Windows:** with Tesseract, run the two sample-image parity/process-count tests and one live
      story. Require unchanged speaker/text/confidence/profile/attempts, working
      output-file cleanup and OMP limit, fewer process launches and lower median
      latency than the separate TXT/TSV path. The macOS 11-frame check passed
      with 48 to 33 launches and 1392 to 941 ms median latency.

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

## Deferred

- [ ] Prepare the application for distribution.

## P2 - Qualify the real desktop experience

- [ ] **Validate on clean machines:** on clean macOS and Windows installs,
      auto-detect the game (with folder fallback), select a game narrator, preview
      and save it, restart, then verify that live fallback and offline preparation
      both retain the choice.
- [ ] **Validate manually:** exercise the main player journeys on real displays:
      fullscreen and multi-monitor use, focus loss, rapid advancement and shutdown during each pipeline stage.
      Check Stories, Voices, Reading, setup and Settings for visible primary
      actions at small sizes, exact clipboard copying, keyboard navigation and
      contrast when switching system light/dark themes while the app is open.
      In Settings, reach the final Advanced speech field and restart note by
      keyboard; focus must scroll into view while Save stays
      visible. Confirm Cancel discards a cross-section draft and Save persists it.
- [ ] **Validate story-match recovery on macOS and Windows:** with an unmatched
      visible line and the smallest supported display, use
      only the keyboard. Require all three actions and their focus to remain visible and distinguishable,
      Enter to start live OCR reading where supported or retry matching in sequence
      modes, Stories to open with reading stopped, and Escape to leave reading
      stopped. Confirm emergency stop dismisses recovery and a settings restart
      invalidates the old choice. Retain an active-window/full-display recording
      showing visible primary-button focus.
- [ ] **Validate Readiness on macOS and Windows:** use
      only the keyboard through loading, an OCR error, a voice
      warning and success. Require Full check details and its text area to have visible keyboard focus,
      the full long error to scroll to its end, the install guide to open the
      official Tesseract page, cancel/retry to show no stale result, and Open
      Voices/Open Reading to return focus to the correct destination. Retain a
      short recording.
- [ ] **Validate macOS permission recovery on a real host:** deny and grant Screen
      Recording and Accessibility independently, return from each System Settings
      pane, and verify automatic status refresh. Check the unknown-status and
      request-failure paths, restart after a newly granted permission, then
      confirm Reading can capture text. Both paths and
      Refresh/Close must remain keyboard accessible.
- [ ] **Validate the asset manager on real macOS and Windows hosts:** in light and dark themes, use the keyboard to
      choose, checksum-validate and save an existing voice manifest; import a
      manifest and one character voice; verify, download, cancel and retry an
      XTTS model. Confirm changed model selections never inherit another
      model's ready state and that failure text stays readable. Verify complete
      error/path copying, unsaved selection cancellation, imported-file permanence
      after Cancel, and close during import. Automated Qt checks and native macOS
      library validation cover these states without qualifying real network transfers.
- [ ] **Validate game profiles on real macOS and Windows hosts:** reach the full selected-settings summary by keyboard scrolling,
      distinguish same-prefix profile names in the open selector, and confirm
      every action remains visible. Keyboard focus must follow selection/management/activation order;
      unavailable actions appear disabled, and Close does not imply
      rollback of saved profile edits. Simulate a read-only region file and full
      settings volume to verify activation leaves the previous profile usable.
- [ ] **Validate manual voice import on real macOS and Windows hosts:** use the keyboard
      to enter required character/optional aliases, select
      and replace audio files, inspect a long scrollable file list, cancel without
      import, and confirm an import error preserves the entered selection for retry.
- [ ] **Validate calibration on real macOS and Windows hosts:** select one complete dialogue box (name,
      multiline text and continue indicator), move/resize it, review and enlarge
      the capture, scroll to its edges, and cancel without changing prior settings.
      Include near-fullscreen/top-positioned dialogue and real game fonts. Deny
      region-file write permission, then restore it and retry. Confirm only the
      active profile (or global region without a profile) changes on Save and old
      manual OCR subregions clear only after successful saving. Retain native
      screenshots, focus evidence and before/after config values.
- [ ] **Validate uncertain OCR review on real macOS and Windows hosts:** correct one speaker-only and one text-only sample, inspect a
      wide/tall screenshot with both scrollbars, retry after read-only storage,
      and confirm the rule reaches only its selected game scope. Retain screenshots
      of compact, error and zoom states plus the saved correction entries.
- [ ] **Validate OCR correction editor on real macOS and Windows hosts:** add/edit/delete rules in both scopes, hover and keyboard-edit
      a long clipped value, cancel an uncommitted cell edit, inspect first-error
      tab navigation, and retry after a denied write. Keep screenshots of compact,
      validation and discard-confirmation states plus the persisted rule file.
- [ ] **Dialogue history on Windows:** start a captured line with its assigned
      voice, select or filter to another line, then stop and confirm the audio
      and named status agree. Restore an unavailable engine and retry from the
      same window. Check keyboard access to playback controls,
      closing with Escape, and scrolling and copying the complete error.
- [ ] **Validate diagnostics and support on real macOS and Windows hosts:** inspect the capture summary, expand separate speaker and
      dialogue OCR images/confidence, and enlarge each image with both scroll
      axes. Trigger permission/window warnings and a timeout; verify stale data
      stays dated, full warnings can be scrolled/copied, and Refresh and its remedy
      remain reachable by keyboard. From one successful native capture,
      confirm the original, speaker OCR and dialogue OCR enlargements belong to
      that same snapshot, then retry after timeout and restored permissions.
      Browse a growing log without losing selection, export and
      inspect a support archive for screenshots/dialogue/audio/secrets, then
      retry after a read-only destination. Retain screenshots and archive inventory.
- [ ] **Validate the authoring workbench on real macOS and Windows hosts:** choose collections with and without pending lines,
      generate one line, stop playback before the end and confirm Approve/Reject
      stay blocked, then finish playback and approve. Reopen and replay the
      approved recording, including while another window owns generation;
      confirm review decisions wait for the lease, saved review filters remain
      independent of collection selection, and the full selected dialogue text
      stays visible and accessible by keyboard. Change collections
      during reload and confirm Generate/Retry wait for the new scope. Stop a
      real generation process, including Stop and close, and confirm the window
      closes after termination. Trigger a workspace load failure, scroll/copy its
      complete literal diagnostic, restore the input and Retry; confirm stale
      counts disappear and recovered controls show their current state.
      Retain screenshots, native focus and playback/decision evidence.
- [ ] **Validate cohort review on real macOS and Windows hosts:** listen to every required sample to the end, mark one unclear
      defect and replace it with a specific reason, accept a clean cohort,
      reject a bad cohort, request more evidence when available, and leave
      undecided. Trigger a bundle-load failure and verify the blocked status and
      Retry are visible without scrolling; confirm keyboard focus order
      and that checksum or stale-authority errors cannot commit a decision.
      Stop while a recording is preparing and switch samples during playback;
      confirm no delayed sound or false heard mark. Close during loading/saving
      and verify completion, then force a listening-progress write failure and
      retry or explicitly discard. Retain native audio-stop and focus evidence; offscreen callbacks do not qualify physical output.
- [ ] **Validate blind missing-voice review on real macOS and Windows hosts:**
      compare two playable A/B candidates, stop each
      before the end and confirm Neither/Choose remain blocked, then hear both
      fully and save one choice. Repeat with one failed candidate and with both
      failed; require the read-only automatic unresolved summary, keyboard access, and no disclosure of hidden source identity before
      import. Confirm a mixed multi-family session counts manual and automatic
      outcomes separately after reopen.
- [ ] **Validate failed-reference audit on real macOS and Windows hosts:**
      use the keyboard to switch groups,
      candidates and affected lines in both directions; confirm the full line
      remains readable and focus scrolls into view. Interrupt original playback,
      switch selection during playback, fail source preparation and generate or
      cancel a preview. Only full original playback of every candidate may enable
      either decision; a preview must never count as original evidence. Save and
      reopen to confirm the selected reference and progress persist.
- [ ] **Validate source-reference quality review on real macOS and Windows hosts:**
      inspect the source identity/portrait, switch generated
      samples, copy their full text and use the keyboard to reach
      playback and decisions. Stop original and generated audio early and switch
      samples mid-playback; neither action may count as complete listening. After
      the original alone, Reject and Need another should enable while Accept
      remains blocked; after every generated sample, Accept should enable.
      Save and reopen a final decision and confirm the completion message stays
      visible.
- [ ] **Validate blind A/B listening on real macOS and Windows hosts:** read and scroll a long dialogue, use the keyboard
      to reach A, B, Review context and all four decisions.
      Confirm a pause/resume preserves only natural full-playback credit, seeking
      or stopping does not grant credit, and automatic playback starts B only
      after A finishes. Save A/B/tie/neither outcomes; verify no model is called
      a leader for tied or all-neither results, the report button receives focus,
      and the saved report opens in the default application.
- [ ] **Validate terminal-conflict review on real macOS and Windows hosts:**
      read a multiline affected line, move by keyboard
      through Play A/B, Review context, and all three outcomes.
      Stop playback early and verify no listening credit; natural completion of
      both recordings alone must unlock Keep A/B and Neither. Save a decision,
      reopen, and verify progress and unchanged source workspaces. A dangling
      progress artifact must block opening rather than present a fresh review.
- [ ] **Validate rejected-recording reassessment on real macOS and Windows hosts:**
      hear two recordings to their natural ends, stop one
      early, choose multiple defects and then acceptable, navigate both ways,
      and publish. Confirm focus follows the scrolled final defect.
      Repeat after read-only progress and publication folders, then reopen to
      verify only saved choices persisted and earlier decisions remain intact.
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
      SoundEffect v2 on a CUDA BF16 host using the locked runtime, the pinned
      checkpoint, a small fixed corpus and multiple checksum-bound seeds.
      Require technical and blinded perceptual approval before adding a provider;
      unsupported effects remain explicit omissions.
## Active - Voice-picker accessibility verification

- [ ] On supported macOS and Windows desktops, exercise the selected-story voice
      overview at minimum width using the keyboard.
      Traverse the table, selected reference details, More actions and Preparation
      details; require full values/actions to be reachable, the selected group to
      stay identifiable, and Copy to preserve complete details. Choose a voice through
      a canonical character and through a linked name; require one shared choice
      in other stories, the correct voice in audition, no assignment selector and
      reachable clear/unlink actions. Existing prepared audio must stay unchanged.
      Change to an imported
      narrator and listen before Apply voice changes; require the current narrator's
      original recording. Apply the changes before opening character audition. Retain native screenshots and an audio trace.
- [ ] On supported macOS and Windows builds, traverse
      target, source, game-reference, consent, preview and action controls;
      enter the same editor from live character recovery with that character selected.
      Confirm an unknown character in live and Read once uses the narrator without
      opening a blocking choice. After a live voice edit, exercise Save, Back,
      Resume with narrator and Escape; Stop or a failed/cancelled voice reload must
      never restart reading. Exercise the same optional edit after Read once and
      require Cancel without a paused-recovery prompt.
      Trigger original and preview playback states and confirm keyboard focus follows visual order.
- [ ] In Preparation voice audition, use a source with a known title and transcript;
      require ordinary evidence to remain visible outside technical details. Exercise
      in-place Play/Stop for original and preview, switch candidates during playback,
      and inject a delayed save failure while pressing Back. Gate: stale audio stops,
      Back waits for persistence, Retry saves the same visible choice, and the replanned
      route matches that choice. Repeat the flow by keyboard.
      At minimum width, open both source and phrase dropdowns near
      the display edges; require visible options and a keyboard return to the
      selected control. Browse all 18 references, compare Ready with speech-check
      warnings, copy a long status/error, and verify Play/Stop labels and control
      positions. Retain real desktop screenshots on both OSes; virtual Qt
      geometry alone does not establish native screen-edge behavior.

## Active - Offline preparation hardware acceptance

- [ ] On supported macOS and Windows model runtimes, start one-shot and live
      Reading while a speculative reference preview is queued and while it is
      rendering. Require cancellation to be requested before reading starts,
      queued work to be removed, no background restart during startup, speech,
      pause or stopping, and eventual model release without blocking the window.
      Retain runtime cancellation/timing logs and audible playback evidence;
      coordinator regression tests use a controlled renderer, not the real model.

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

## Active - Offline generation retry deadlock

Archive (20) retains only historical October 14 lock-wait stacks for this incident. Its October 21 native requests return four bounded frame-limit outcomes; they do not establish a new deadlock or qualify current Cancel behavior.

- [ ] Verify on the reported Windows/GPU setup that a frame-limited phrase reaches its bounded retry result and Cancel finishes. Incomplete audio must remain unpublished. Local Python 3.14.2 HTTP-fixture and retained-frame regressions pass; actual model EOS quality remains a separate hardware gate.

## Remaining - Shared audio storage

- [ ] Qualify shared WAV hard links on the supported Windows filesystem.
      Keep explicit same-filesystem/link errors and existing audio intact; do
      not add a silent copy fallback.

## Qualify SQLite-only storage

- [ ] Qualify the SQLite-only publication, interrupted publication/retry, hard links
      and catalog-loss refusal on Windows. Keep current
      immutable authoring proof files and portable manifests intact; those are
      supported contracts, not working-state fallbacks.

## Deferred - Optional status-message design review

- [ ] Consider replacing the global last-message strip with context-appropriate
      feedback: reading state beside reading controls, generation progress in
      preparation, local errors beside the affected action, and technical events
      in diagnostics. Reserve a top banner for important application-wide problems
      that remain relevant until resolved; ordinary updates must not overwrite
      them. Avoid confirmations when the interface already makes the result clear.
      This is an optional design idea, outside the chapter-run defect queue and
      acceptance gates. Agree a concrete scope before implementation.
