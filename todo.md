# TODO

## Active - Finish diagnostic-session native acceptance

- [ ] Preserve usable diagnostics after process/OS interruption. A native run left
      `session.json` at its initial preparing state, omitted source evidence and
      produced an unreadable MOV (`moov atom not found`) despite persisted event
      journals. Persist recorder identity/readiness and source references during
      acquisition; use recoverable video fragments or bounded finalized segments.
      On startup mark interrupted sessions explicitly. Gate: abrupt termination
      retains a playable completed segment and honest partial-analysis coverage.
- [ ] Diagnose simultaneous process-tap sample-time discontinuities under live
      chapter generation. Both game and Rust taps stopped around 114 seconds while
      Reading continued, each reporting `Sample-time discontinuity`. Preserve the
      previous expected and current sample/host timestamps and device-change
      evidence at the failure. Do not infer playback underrun from recorder failure;
      qualify recovery/coverage in a fresh uninterrupted native run.

- [ ] **D1 - Finish recorder turnover and bounded-stop qualification.**
      After forced Rust-worker exit, the repaired session retains a partial segment,
      explicit worker identity/loss evidence and independent game video/audio plus
      journals until the original deadline. Verify a replacement worker within the
      same session and actual default-device change. The corrected bounded-stop
      contract passes a native deadline run with both process taps finalized and no
      acquisition errors. Wait for media readiness before starting Reading: cold
      native-tool preparation took58s of a300s session and missed the first clip.
      Qualify captured intervals without claiming uncovered startup/tail time. Private aggregate-tap
      creation alone did not reproduce device-refresh feedback; retain refresh-origin
      events and choose an audio-owner repair only if evidence supports it.

## Rust audio native acceptance gaps

- [ ] Complete adverse-path qualification of `scripts/mac_live_recording/process_tap.m`:
      native permission denial, worker exit/restart, device change and reverse
      teardown. The positive-path native gate now has exact-source PCM correlation
      and full 300s/180s exact-window recordings; do not repeat it as an untested
      prerequisite. Keep explicit PID/path/start time/HAL ownership and no parent,
      desktop or global-audio fallback. Investigate ScreenCaptureKit -3805 lifetime
      interference only if serialized ownership reproduces it. See R12 for the
      remaining source/output discontinuities; positive capture is not gap removal.
- [ ] Validate the Rust owner in the real game with genuine concurrent heavy chapter
      generation/publication, using the same approved WAVs, device, rate and
      latency as the retained Python baseline evidence. Controlled GIL stress
      establishes feeder isolation, not the cause
      of the historical app underruns. Do not claim audible gap removal or full
      Mac R12 acceptance from callback counters alone. Include native Pause/Stop,
      restart, worker crash and actual default-device-change checks; retain the
      existing interrupt/requeue Pause behavior rather than implying a new seek.
## Reading setup blockers found during persistent-OCR preflight

- [ ] Verify Stories-page scrolling and action reachability in compact-height windows.
      At860x560 the story list and preparation actions extend below the visible
      scroll area; MCP wheel events on both the scroll area and viewport leave its
      scrollbar at0 despite maximum285. Determine whether this is an app wheel/
      layout issue or MCP event routing before changing behavior. Gate: a user and
      MCP can reach story selection and Prepare without resizing or targeting
      clipped widgets; preserve ordinary keyboard navigation.
- [ ] Allow prepared-audio Reading setup to validate its actual source route.
      The final wizard page currently requires successful live synthesis before
      saving, even when prepared recordings exist. Imported narrator + the default
      Pocket fallback failed with the gated-model error. Check the selected
      prepared/original route and audio output independently; retain explicit
      fallback readiness/error reporting and do not silently mark setup complete.
      Gate: existing prepared first line can complete setup without generating it
      again, while missing or invalid prepared audio remains actionable.
- [ ] Reject off-screen game geometry before calibration capture and ask to show
      the selected game. AeroSpace can park an inactive workspace window at
      (1727,1089) while Quartz still reports it onscreen on a1728x1117 display;
      calibration then accepted an almost entirely off-screen area and displayed
      a blank preview. Require usable display intersection and revalidate after
      workspace/window changes. Gate: no blank capture is offered for approval,
      and activating the game permits normal calibration with the same target.

## Persistent OCR remaining qualification

- [ ] Run the prepared-dialogue + background-generation scenario on the current
      Mac with the persistent OCR worker. Include Stop/restart, process failure
      and actual mixed-language game frames. Wait for game/window/Rust recording
      readiness before Start; verify real generation progress and prepared/original
      routes, then Pause/Stop/restart. Saved-frame parity and calibration recognition
      do not qualify live timing.
- [ ] Separately verify native library/model loading on Windows and a clean
      packaged Mac; offline process parity and mocked DLL layouts do not qualify
      these platforms. These checks do not block the current Mac trial.
- [ ] Choose multilingual model sets using labelled genuine game captures with
      Russian within English lines, French/German accents, partial text and moving
      names. Keep current configured languages until quality is qualified.
- [ ] Retain the command-line OCR path only as an explicit benchmark/parity
      reference while native platform qualification is unfinished. After Windows
      and packaged-Mac parity pass, remove the CLI reference backend/helpers and
      replace their comparison tests with the qualified fixed fixtures.

## Active - Qualify portable observation changes

- [ ] Repeat the native prepared + background-generation run after the fixed-anchor
      render/equivalence repair. Measure completion-to-key and next-glyph-to-audio
      alongside CPU; verify real punctuation/prefix growth, moving names, overlays,
      Pause/Resume/Stop and no stale occurrence or duplicate dispatch. Offline
      synthetic regressions and retained frame equivalence do not qualify timing.
- [ ] Evaluate a persistent MSS capture owner per Reading session on Windows and
      Mac. Preserve before/after window geometry validation and capture provenance;
      close only after the capture worker drains and recreate on target/settings/
      display changes (MSS caches monitor topology). Compare against per-frame MSS
      construction before choosing a resource-lifecycle change.
- [ ] Qualify the selected OCR candidate with partial/full text, narrator and
      speaker changes on a labelled corpus, then under concurrent generation.
      Preserve confidence/uncertainty handling, source routing, cancellation and
      language coverage. Mac offline latency/CPU does not establish Windows speed;
      parent-process CPU cannot compare Tesseract subprocesses with in-process OCR.

## Investigate - Reduce observation latency before choosing an OCR replacement

- [ ] Separate capture-to-observation latency into frame age/queue wait, region
      preprocessing, each OCR invocation/profile, canonical matching and admission
      waits in built-in diagnostic journals. `ocr_ms` exists in the current UI
      snapshot but accepted `ocr` timeline events do not retain this breakdown.
      Keep capture IDs, occurrence/owner and monotonic timestamps; cached/canonical
      observations must not count as fresh OCR. Gate: replay reports distinguish
      actual recognition time from stability waits and first PCM submission from
      captured audible onset, including rejected/uncertain attempts.
- [ ] Check whether Reverse:1999 exposes a usable runtime dialogue event/line ID,
      occurrence and render-complete signal. Static extracted story order is not
      runtime position. Bound the feasibility investigation before building an
      adapter; require evidence across transitions, repeats, choices and resume.
      Preserve focus/Stop/ownership gates. If no reliable source exists, retain
      visual observation and document the specific unavailable signal.
- [ ] Qualify RapidOCR with crop-appropriate detector scaling before rejecting it.
      Assert actual tensors: the default short-side736 rule expands narrow game
      ROIs drastically. Max-side mode restores the missed Cornerstone name but
      currently loses the ellipsis in "Well, um ..."; fix/qualify punctuation and
      long-line latency before production selection. Compare tiny/English models,
      known speaker-line recognition without detection, blank-name early rejection,
      and bounded CPU threads/spinning settings against persistent Tesseract.
      Change one factor at a time and retain per-stage timing and raw detector/
      recognizer results; language="eng" on the wrapper does not select an English
      RapidOCR model. Expand the labelled corpus with partial prefixes, repeated text, raised
      names, punctuation and animation. Compare wrong occurrence/speaker,
      missed/duplicate transitions and p50/p95 wall/total CPU under concurrent
      generation. Keep OCR choice independent of capture lifecycle and R18;
      accept a replacement only on measured improvement without relaxed admission.

## Current scope and execution order

Target: finish the Mac Reading follow-up by fixing every defect found in the prior
Mac screenshot/video/live trial, then repeat the Mac run with real per-character
heavy-voice preparation. Exercise **original game voice -> prepared recording ->
live fallback** while chapter generation is active in the background. Start a
known prepared line during typing as soon as fresh, sufficient evidence identifies
its occurrence; the first glyph can be sufficient only for an owned, deterministic
successor, never arbitrary text. Windows acceptance remains a separate future
platform gate and does not block completion of this Mac work.

**Ready** means implement in the shared product owner with caller/UI/test coverage;
**Investigate** means obtain the missing causal evidence before choosing a repair;
**Validate** means run the stated real-host acceptance, not assume a new defect.
Refactor ownership/state contracts or change the UI where necessary. Do not fix
these findings with launcher monkey patches, hardcoded story edges, wider global
matching, weakened admission, extra key presses or larger timeouts.

Remaining execution order:

1. Finish the remaining native calibration interaction gates (U1/U4), retaining
   the maximum multiline dialogue height, raised nameplate and continue cue.
   The corrected14 ->15 early-admission gate passes; do not restore the clipped
   one-line region or treat it as a matcher defect.
2. Investigate independent owners in parallel: OCR-worker health/stage callbacks
   and truthful retention gaps (R1/T1); prepared audio starvation at the real
   native/Python boundary (R12); original-source elapsed/completion evidence (R6)
   and the separate centered layout (R17). Inspect shared callers, current logs,
   source/graph metadata and tests before choosing each repair. Keep setup errors,
   missing measurements and demonstrated code defects distinct.
3. Record each demonstrated repair's files, dependencies and regression gates here,
   then implement it in the shared owner with its callers/UI/tests. Preserve
   occurrence/source/WAV integrity, stale-run/focus/epoch/Stop guards and bounded
   cancellation; no guessed edges, global threshold relaxation or arbitrary
   timeout/buffer growth. Serialize changes to overlapping owners. Run the local
   changed-test selector first after code changes and complete relevant static
   checks before any native performance measurement.
4. Prepare the native tools and evidence: qualify explicit-target MCP, calibration
   visibility and control identity/lifecycle (T3/T4/T5/U1/U4), retain early
   timing events and record instrumentation overhead (T1). Keep game window,
   process audio, original/generated artifacts, model/runtime identity and the
   approved-WAV baseline explicit. Native exact-window video may follow origin-only
   moves of the same PID/window/size with explicit movement evidence; owner, target
   closure and size changes must fail closed. Desktop-rectangle product capture
   still revalidates its geometry. No desktop fallback; partial recordings cannot
   pass a full-run gate.
5. Qualify the existing implementations on Mac: completed-text freshness and safe
   early successor start (R2/R3), published pack/Bob Narrator fallback (R4/R11),
   explicit indexed recovery (R5), Read-once/transition/uncertainty ownership
   (R7/R8/R9/U9), cold readiness/fallback startup and offline assets (U6/U7/U8).
   Add a code repair only if its targeted check establishes a remaining defect.
6. Run M0 live fallback and M1 prepared per-character playback with real pending
   heavy generation, publication and catch-up, after the code gates. Measure
   14 ->15,16 ->17, original speech completion, gaps, continuation and Stop against
   independent exact-window video/process audio. Preserve approved WAV bytes;
   cache hits alone do not qualify concurrent generation. No unit-test load runs
   during the measurements; no separate generation-idle prepared run is required.
7. Analyse source/output timing and gate outcomes, remove only completed verified
   TODO items, and retain concrete remaining defects with reproduction and next
   evidence/repair gates. Windows and unrelated authoring/package work stay in
   their existing deferred sections and do not block this Mac follow-up.

## P0 - Repair Reading ownership, OCR and continuation

- [ ] **R17 - Finish centered-source coverage verification.**
      A complete native repeat shows44 ->centered46 ->bottom47 continuing without
      an app key between44 and47;47 is admitted normally. The bottom ROI truthfully
      did not recognize46. This episode does not justify an alternate crop as a
      continuation repair. Confirm the original46 audio once against a trustworthy
      exact source: current mixed-output NCC.534 is insufficient, as is47's.617.
      Do not count a declared source duration or visible center text as observed
      audio completion. If another centered occurrence actually requires reader
      playback/input, trace installed type8/center markup through extraction/index
      before selecting an explicit bounded layout; preserve stale-text/overlay,
      occurrence and source guards rather than widening ordinary calibration.


Reproduction baseline: Mac experiment on build `a4d27286`, Reverse:1999 English
stage314001, original-first hybrid policy, only lines6/7 prepared, otherwise Pocket
fallback. Findings below include the relevant observations and reproduction gates;
local recordings/logs are not prerequisites for taking a task. Reproduce with a
fresh capture or checked-in regression fixture when evidence is missing. Keep
uncertain causes explicit; this baseline does not qualify Windows OCR or audio.
Shared Reading owners are `vntts/live.py`, `vntts/controller.py`,
`vntts/dialog_capture.py` and `vntts/generated_audio.py`; UI state is in
`vntts/app.py`, exported timelines in `vntts/support.py`. Update the relevant
callers and focused regressions together when changing these contracts.

- [ ] **R1 - Investigate the capture-only stall using independent OCR-worker health.**
      After accepted frame2676 at monotonic148117.451, OCR/gate events stop for
      over 100s while capture continues. Use recorded OCR future state/error type
      and stage-progress age, then capture thread stacks before Stop; identify the
      blocked callback or exit before implementing recovery. Gate: a dead/stalled
      OCR worker cannot present healthy Reading indefinitely, failure/retry is
      actionable, Stop remains bounded,
      and recovery cannot create a second worker or replay a claimed occurrence.
      Use queued/running health, callback stage and consumed/latest frame versions
      to locate the actual native stall. A blocked comparison must leave health and
      Stop responsive; revalidate frame/epoch/owner before admitting its late result.
      Offline regressions do not establish the cause of the historical capture-only
      stall or justify automatic recovery/new timeouts.
- [ ] **R2 - Validate completed-text freshness on Mac.** Use completed left-edge
      `Ouch!`, absent names, changing brightness, animated cues and 1-2px auto-ROI
      translation/size changes with capture faster than OCR. Require prompt current
      recognition while real replacement, changed name, new punctuation/typewriter
      glyphs, focus/owner changes and Stop still reject stale speech. Retain captures
      showing whether real antialiasing changes conservatively need another OCR pass.
      R1's later capture-only stall remains a separate unknown cause.

- [ ] **R3 - Validate owned early prepared-successor playback on Mac.** Use a
      fully admitted per-character recording and an authorized deterministic key
      transition, including an uncertain old narrator frame before the first valid
      prefix. Require one correct WAV start during typing and no new synthesis;
      reserve/prebuffer before onset. Check wrong names/text, common or ambiguous
      prefixes, missing audio, manual input, focus loss, Stop/pause, branches and
      manual boundaries. Uncertain frames cannot authorize speech; ordinary
      non-sequence prefixes still need unique sufficient evidence.

- [ ] **R4 - Qualify the published chapter in Mac Reading.** Use the portable
      pack admitted by the normal publisher/importer: original4, prepared90,
      omitted1, missing0. Verify current index, semantic evidence, graph and
      progressive manifest in the player; original -> prepared -> live priority
      and Bob's standard Narrator fallback must preserve speaker/line identities.
      Gate: resume and catch-up keep approved WAV bytes, omissions and original
      coverage truthful while Reading continues. A cache-hit-only run does not
      qualify genuine concurrent heavy generation; no separate generation-idle
      prepared run is required.
- [ ] **R5 - Validate explicit indexed-current recovery on Mac.** For an exact,
      unique indexed line outside the expected graph, use Read indexed current line
      from Reading, compact controls and keyboard/tray without advanced settings.
      Confirm configured original -> prepared -> live priority, one explicit
      re-anchor, manual continuation, and no guessed edge, silent skip, duplicate
      speech, stale/blank-frame recovery or restart after Stop/cancel. Distinguish
      this action from unrecognized text and an explicit position-picker choice.

- [ ] **R6 - Investigate: account for elapsed original speech before completion.**
      Native reference 47 ends 148093.107, route selection 148093.559, completion timer
      fires 148094.402 and key 148094.765. Identify a trustworthy onset/elapsed basis
      before replacing the late full-duration+350ms wait in the shared route owner.
      A new prepared/MOSS Mac run correlates original68's waveform at about.964:
      its digital clip starts199678.777, route199679.773, timer199679.809,
      clip ends199680.435, key199682.288. The full-duration timer restarts late.
      Do not simply subtract time since the first visible glyph: original47's text
      is visible by199521.9 but its voice starts199522.041, so that timestamp is
      not a safe upper bound on audio onset. Establish an authoritative per-game
      onset/completion observation or bounded onset contract first. Carry it frozen
      with occurrence/run/epoch and capture identity through chunk/route ownership;
      stale/future/unknown evidence must preserve conservative completion, and
      explicit replay must not reuse an old live deadline. Inspect both full source
      pass-through and partial-source lead waits in the shared route owner.
      Distinguish observed completion, conservative estimated remainder and unknown
      duration; retain partial-source lead and focus/visual guards. Gate: a known
      already-ended original does not restart its whole duration, active source
      speech is never cut off, and unknown timing cannot be declared measured.
      Source inspection found no existing runtime onset/end authority: cue/render
      quiet are text evidence; the index binds exact media duration, not playback
      start. Plan the smallest source-end observer first, using explicit owning-
      application digital audio plus checksum-bound source PCM. Confirm an actual
      already-ended occurrence rather than extrapolating a future deadline from
      onset; reject repeated/ambiguous clips, other-window mix, mute, discontinuity,
      stale run/epoch/lease and missing exact source references. Observer-only native
      comparison on47/68 must pass before it may shorten either full/partial/pending
      source waits. Unknown evidence must retain the conservative existing route.
- [ ] **R7 - Validate shared Read once lifecycle on Mac.** Natural finish,
      failure, Stop, cancel and recovery must clear Speaking/Stop in Reading,
      compact and tray controls. A displayed Stop and direct controller toggle must
      never start another read; late OCR/playback cannot overwrite a newer read.
      Retry Settings after a stop timeout while speech or cancelled OCR still drains;
      it must wait without opening/applying early or restarting capture.

- [ ] **R8 - Validate transition/typing status with fresh Mac run IDs.** For
      long successor typing, require ongoing typing or recognition wait instead of
      an ignored-key error. An unchanged new frame with unavailable OCR must reach
      an actionable unavailable status; harmless old-frame jitter must not prove
      a transition. Confirmation must resume without duplicate audio or another
      key after a timeout. Keep historical Windows stalls as separate deferred
      platform qualification.

- [ ] **R9 - Qualify uncertainty recovery and control ownership on Mac.**
      Verify line16 ->17 advances once after fresh uncertain OCR, without manual
      Space or replay. Exercise uncertainty before/during/after speech, stale
      run/owner/epoch/focus and Pause between visual admission and key dispatch.
      Pause/Resume must continue once; Clear/Stop must stay suppressed after exact
      OCR/focus return. Preserve occurrence and WAV/source identity; unowned legacy
      speech must still cancel on uncertainty.
- [ ] **R11 - Qualify retained-backend speaker announcements on Mac.**
      Bob must use his standard Narrator recording while the configured name
      announcement succeeds. Require one announcement and one unchanged dialogue,
      bounded interruption and no orphan worker. A genuine announcement error
      must still preserve dialogue playback.
- [ ] **R12 - Investigate short output discontinuities under real MOSS load.**
      On Mac build fee2f818, prepared stage314001 with concurrent MOSS generation,
      exact approved-source correlation found line 8 +192 output frames (4.354ms
      at 44100Hz) inside an existing quiet span, and line 54 +960 frames (21.769ms)
      during speech, including a new 16.984ms exact-zero span. Rust reported no
      underflow and both explicit-process tap streams had continuous timestamps
      and no queue loss. Most other energetic windows retained alignment within
      0.125ms. These observations do not yet identify Rust, CoreAudio/device or
      capture as the source. Reproduce the same approved WAV/device/rate under
      controlled heavy load, correlate source/output and native counters, then
      capture time-indexed stacks separately from clean performance acceptance.
      Do not count intentional source lead waits (line11/20/38:1350/3466/1213ms),
      speaker announcements or original WAV silences as underruns. Stop's native
      positive path is measured at 81ms to last nonzero output; retain remaining
      worker crash/restart and actual default-device-change gates.
      Compare historical Python evidence only with matched source/device/rate;
      no GIL-only attribution, launcher monkeypatch, WAV editing or buffer inflation.
      Disambiguate repeated identical WAV occurrences before treating an NCC peak
      as an authoritative onset; a matching source alone does not identify playback.

- [ ] **R18 - Validate visual-ownership repair and trace remaining long waits.**
      Mac build fee2f818, stage314001, prepared voices + background MOSS: after
      line 15 playback completion, key dispatch waits 10.24s; after 16 it waits 18.99s.
      Exact-window video shows the same complete text, name and continue cue;
      focus remains owned and canonical-full-text was confirmed before completion.
      Stable-frame readiness flickers and fingerprint changes accumulate while
      the character/background animates. Use Support's diagnostic session and its
      symbolic refusal/prefix-barrier snapshots plus routed/latest fingerprint
      equality to trace the actual mask/owner which invalidates admission. A repeat
      with built-in journals proves the immediate refusal gate: visible/focused/cue
      are true, speech queues empty, but routed/latest equality is false. Ordinary
      completion-to-key waits span0.438..2.058s; after Resume on line40 a separate
      4.975s wait remains after completion (exclude the intentional30s pause).
      The shared gate now compares against the fixed routed anchor using existing
      frame equivalence and rechecks focus/epoch/queues. The next native run still
      found a4.248s wait on42 and2.936s on68; all12retained mismatch pairs reject
      strict equivalence. Verify the shared fixed-anchor comparison and atomic
      render-quiet binding on the real game. Native retained pair68 now compares
      equivalent on shared bounds, including its217->218 dialogue edge and dim
      name edges; actual prefix42 remains different. New punctuation, changed
      names, overlays and cumulative edge drift must still invalidate evidence.
      Trace current-line confirmation/rebinding after early prefix playback: on42,
      full text was visible297ms after playback but confirmation took3876ms.
      The render anchor now survives supported edge noise; establish whether that
      removes the remaining wait before making further changes. Repeat15 ->16,
      16 ->17 and Pause/Resume with intermediate render-reset evidence, preserving
      all Stop/focus/epoch/source guards and exactly one key per occurrence.

## P0 - Make capture setup trustworthy and operable

- [ ] **U1 - Validate unobscured setup capture on the native Mac.** Exercise
      wizard and tray calibration with Dashboard, compact controls and a pending
      diagnostics refresh over the game; inspect actual test/calibration pixels.
      Delete/close the wizard while capture is pending: surviving controls must
      return without reopening the wizard or activating diagnostics. Confirm setup
      windows disappear only while pixels are acquired, return before OCR/playback,
      and Cancel remains usable during model preparation/playback. Exercise capture
      failure, cancellation, close and retry; restoration must not steal game focus,
      reopen a closed wizard or start continuous Reading. Inspect/copy the complete
      recognized text/speaker and retain the saved auto-advance opt-out. The setup
      test qualifies OCR/live preview; M1 qualifies the configured hybrid route.
- [ ] **U2 - Validate remaining held-out adaptive-crop cases on Mac.**
      The corrected glyph/background fallback and generated-pixel regressions pass;
      native Narrator6 and Cornerstone15 now have correct dialogue/name handling.
      Retain maximum-height/nameplate-inclusive calibration and qualify additional
      held-out scenes through the existing adaptive-crop/speaker-presence owner.
      Gate: narrator scenes, named speech, short/punctuation-only/1-4-line text,
      scenery behind absent names, faint/unanchored/clipped labels and full continue
      cues pass held-out Mac captures without lowering confidence or clipping
      text. Qualify local-background-check and fast RapidOCR latency tails before
      any backend switch; development controls alone do not establish general accuracy.
- [ ] **U6 - Qualify durable readiness during cold generation startup on Mac.**
      Saved admitted recordings must remain ready while the heavy backend starts;
      Start reading must remain available without new synthesis. Verify original-
      only readiness, cancel/retry and same-input callback handoff. Reject missing/
      corrupt WAVs, foreign state/manifest, changed source evidence and callbacks
      from another input; completed counts alone are not playable coverage.
      On the native compact UI, confirm cold startup names Load reading engine or
      Set up reading, active preparation reports loading, and retry errors preserve
      Check readiness rather than reverting to an indefinite starting message.
- [ ] **U7 - Complete model-asset evidence and native offline reuse.**
      Qualify normal GUI/preflight with network unavailable and no experiment-only
      environment: offline-first and Reading-first must reuse the same managed
      model/codec/cache. Preserve explicit cache overrides and Pocket bundle/token
      isolation. Inspect resolved model/codec and download/load phase reporting;
      add owner-level evidence if only generic loading status remains. Missing/
      corrupt assets and bounded download cancellation/retry must stay actionable;
      do not globally disable Xet.
- [ ] **U8 - Qualify prompt prepared Reading with cold Pocket fallback on Mac.**
      Start reading must capture/play admitted original/generated routes while
      fallback model preparation continues. Later Bob announcement and genuine
      live misses must use the same ready worker and report any actual wait.
      Exercise Stop, Resume, reconfigure, failed-load retry and close during model
      load/warmup; no stale publication, duplicate worker or orphan process.
      Qualify compact opening without game-focus activation using owner-only focus
      evidence. Continue genuine heavy character generation concurrently; native
      MOSS generation readiness is separate from the Pocket fallback startup.
- [ ] **U4 - Validate exact region controls on the native Mac.** Use keyboard/MCP
      to set Left/Top/Width/Height, retain the maximum-height multiline/nameplate
      envelope for native 14 ->15, inspect dialogue/cue coverage and
      review/save/cancel/retry at enlarged text. Confirm the nonmodal coordinate
      panel remains reachable, values match drag/keyboard movement, stale review
      callbacks cannot save another selection, and closing review cancels its OCR.

## P0 - Establish trustworthy latency evidence

- [ ] **T1 - Validate timing provenance and overhead on Mac.** Retain correlated
      run/occurrence/playback/capture IDs through Stop/Start, identical lines,
      retries, failures, source leads and intentional pauses. Record resolved heavy
      model/codec revision and weight hashes for new synthesis; historical cached
      MOSS items retain an unobserved checkpoint revision when only the repository
      identifier was recorded. A matching cache key is not proof of equal historical
      weight bytes. Compare the measured
      first successful PCM-write return and software completion against independent
      recording; neither is physical DAC/acoustic onset. Original onset stays
      unobserved and its duration timer stays estimated. Split original/prepared/live
      and cache states; measure cold model/codec startup, cached reuse and steady
      synthesis separately, without unit-test load or comparing different texts
      as a speed claim. Do not replace `model_warmth=unobserved` with an inference. Confirm append-only
      events, explicit retention gaps and common monotonic offsets survive export
      and restart, including OCR-free visual successors. Use the built-in Support
      diagnostic session and explicit export, including exact-window/process
      recording when needed. Validate early events, symbolic refusal reasons,
      owner/fingerprint hashes, native callback/DAC clocks, source-WAV hashes and
      fresh-render/cache evidence against independent recordings. Verify restored
      media-session export after restart and actual recorder permission/worker failure.
      Journal-only export/restart has passed with byte-identical session files;
      D1 must pass before treating native media acquisition as qualified.
      Keep generation-owner and actual model-worker PID distinct, with unavailable
      placement explicit. Qualify resolved checkpoint evidence instead of inferring
      runtime identity from generic status or previously recorded cache metadata.
      Preserve truthful cutoff/file-close semantics and measure capture/support-log
      overhead and Stop latency; ordinary logs must retain no pixels/dialogue.

- [ ] **T3 - Finish native screenshot-safe MCP edge cases.**
      The desktop MCP host now launches and controls the app and returns explicit
      widget capture metadata successfully. Keep that path while finishing the
      remaining native rejection checks in `scripts/pyside6-mcp/README.md`.
      From a fresh widget tree, check an explicitly selected minimized native window
      without retargeting. The fresh client already qualifies contract1 metadata and
      missing/hidden/closed/process-stale rejection; retain those guarantees after
      reconnecting and relaunching. No implicit screenshots or desktop fallback.
- [ ] **T4 - Validate Mac product, MCP and video capture provenance.**
      Confirm saved PNG/sidecar, diagnostics and Reading timelines agree
      on capture ID, separate run ID, selected window ID/title/PID, bounds, dimensions,
      monotonic time, method/scope and outcome. MSS window-coordinate captures are
      desktop rectangles and may contain overlaps. Close/minimize/move/resize/reopen
      the selected window and confirm failures and new target identities remain
      explicit; never enumerate unrelated titles or log pixels/dialogue. Validate
      native-video target/frame timestamps and MCP contract1 independently.
      Label window-ID namespaces: Qt WId/Cocoa view handles in MCP are not Quartz
      CGWindowIDs used by native recording; correlate owners without equating these
      numeric values. Product screenshot metadata does not qualify video or MCP.
- [ ] **T5 - Finish native activation of truthful MCP action outcomes.**
      Fresh pinned patch/build regressions now cover queued/dispatched/completed/
      error, a true modal QDialog.exec acknowledgement, exactly-once POST, late
      error correlation, key chords, widget lifetime/property access and real-PID
      shutdown. Qualify the modal-action candidate on a native generated fixture,
      then activate through the normal installer/restart path without interrupting
      a game run or discarding generation. A202 acknowledgement is not completed
      work; inspect current state and existing invocation-ID logs before another
      action. Keep the remaining native screenshot edge cases in T3 separate. For VNTTS close,
      verify bounded real-process exit and report actual returncode; do not infer
      graceful shutdown from quit acknowledgement or bridge disappearance.
      Reproduce numeric-input replacement through MCP: selecting the diagnostic
      duration editor with Ctrl/Meta+A then type_text appended digits (0600) instead
      of replacing them. Native QSpinBox scroll changes the value correctly; fix
      the tool selection/key contract and verify both Qt value and displayed text.

## P0 - Repeat the Mac prepared-voice workflow

- [ ] **M0 - Repeat the Mac live-fallback baseline after repairs.** Reproduce the
      stage314001 setup and original-first hybrid policy with baseline coverage, then
      run all repaired Reading, capture, setup and provenance gates. Record
      model/runtime, voice bindings, asset hashes and device; confirm original ->
      prepared -> live priority, timing from first glyph/sufficient prefix/full text
      to speech, source end -> key -> next glyph/speech, gaps, continuation/OCR,
      Stop, route/fallback reasons and explicit provenance gaps. Retain bounded-run
      video longer than four frames with scene and text changes. This is the live
      fallback control for M1.
- [ ] **M1 - Read prepared character recordings while heavy chapter generation continues.**
      On the Mac game/model setup, use the intended heavy offline model and
      distinct saved character voices. Admit the matching index, semantic source
      authority, sequence plan and generated manifest as one compatible set;
      verify readiness/coverage in UI. Include cold start/reopen, safe early
      prefix, same/changed speaker, common-prefix/short/narrator lines, source
      partials, manual boundaries, re-anchor and pause/focus loss. Require correct
      per-character recordings and original -> prepared -> live priority, with no
      wrong/duplicate/skipped/truncated starts or stuck transitions. Known WAVs
      should start during typing once safely identified; prepared hits must not
      synthesize. Report per-occurrence first glyph / first sufficient prefix /
      full text -> speech, OCR rejection tails, gaps, route reasons, bounds and
      unfinished lines. Split cold/warm and routes; compare median/p95/max and
      faults. Treat 500ms safe-prefix latency (serious at 1s), 300ms excess after
      full text, 200ms avoidable gaps and 300ms auto-key excess as investigation
      triggers, accounting for actor pauses, source silence and gate waits.
      These are UX targets; no separate generation-idle prepared run is required.
      Verify essential chapter generation is distinct from speculative audition;
      cancelling previews for Reading must not cancel the chapter job. If concurrent
      work cannot fit, use explicit bounded scheduling/backpressure and truthful
      pause/wait states; no uncoordinated second model worker or forced thread count.
      The native concurrent-load case is established: 27 fresh MOSS renders plus
      one cache reuse, 424 -> 452 published entries, unchanged approved baseline WAVs,
      45 prepared routes and one original route in Reading through line 56. Retain
      the actual remaining gate: start from a partially prepared active chapter,
      cross ready/newly published boundaries and catch up to the generator under
      contention. Verify missing/not-ready/failed route and voice policy, wait/retry
      without polling storms, atomic admitted publication, no partial WAV reads and
      stable identity for a playing occurrence. Include generation cancel/retry,
      Reading Stop, app restart/resume, changed voice/settings with deferred
      activation, device loss, and Settings/calibration while preparation continues;
      completed lines/stages must not regenerate. Require bounded
      reading/playback/Stop/UI service, measurable generation progress or an explicit
      resource wait, and a final pack matching uninterrupted preparation. Record
      game+model CPU/GPU/VRAM/RAM/disk, queue pressure, underflows and playback latency
      on actual Mac hardware; GPU configuration alone is not utilization evidence.
      Include keyboard/screen-reader status and minimum-width/enlarged-text layout.
      Stage314501 and other Windows hardware/platform checks remain deferred.

## Deferred - Windows support archive (20): defects and diagnostic gaps

Evidence summary: Windows support archive (20), exported 2026-10-21 22:07 UTC, Windows build `4bad9c73`; code paths rechecked on `1f4caa46`. Times below are archive UTC. Keep the already-fixed Settings/selection/original-playback delays separate from these remaining items.

- [ ] **S20.4 - Investigate on Windows: account for remaining voice-plan wall time before choosing a broader fix.** The historical `_create_voice_plan` took 408.915 s: candidate preparation 40.576 s and `voice-plan-store` 368.334 s; about 308.962 s of the latter was not attributed by the retained spans. Capture fresh cold and warm runs with the actual Windows/model workload using correlated `operation_id` timings for settings context, grouping, routing, reference qualification, applying choices, persistence, audition cache admission, queue wait, model startup, render, preview quality and ASR advice. Compare disjoint phases with the inclusive total; do not add inclusive parent and child durations. Inspect cache hit/miss, registry-build, reference-read and reference-preflight counters, and retain any unexplained gap explicitly. Identify the measured dominant work before choosing further reuse/deferment; preserve quality, admitted bytes, explicit choices and responsiveness. Fixture improvements do not diagnose the historical 309 s or qualify deferred Windows acceptance, large-pack finalization or GPU headroom.

- [ ] **S20.6 - Validate selected-window disappearance/recovery on Windows.** Close,
      minimize and reopen the selected game window during Reading. Require a
      recoverable capture/window-unavailable status with debounce and normal recovery;
      unexpected exceptions must remain distinguishable. Use the new run/playback
      evidence before inferring any audible output after window closure.

## Qualify Settings on native Windows and macOS

- [ ] Confirm the Settings opening fix on the reported Windows setup: archive (20) measured two pre-fix Settings pack checks at 3.487 s and 3.120 s, each scanning 1,513 files. After `1f4caa46`, measure click-to-first-frame while idle and while Reading is active; distinguish initial rendering, intentional speech draining and Save preflight. Preserve Save errors and Cancel behavior.

- [ ] With verified background workspace isolation or an explicitly approved native session, edit Settings across sections, open the file and engine selectors, save and cancel nested voice choices, and confirm keyboard focus/VoiceOver or NVDA announcements at enlarged system text. On Windows verify native shortcut symbols and application after Save; on macOS verify the unsupported-hotkey explanation. Start with a saved non-100% speed, reset to 100%, save, reopen, and play an existing prepared line; repeat reset with Cancel and verify the original setting remains. Retain native focus, popup placement and audible playback evidence. Offscreen Qt checks cover persistence, rollback, literal text, navigation and layout, not OS integration or physical output.

## Qualify Reading setup with native capture and audio

- [ ] With verified background workspace isolation or an explicitly approved native session, run Reading setup on macOS and Windows: choose a real game, recover from a missing audio device, calibrate, test/cancel/retry, change the region after success, then save. Confirm actual audio stops, a new area requires a new successful test, cancellation restores saved runtime settings, and Save leaves Reading visible without starting playback. Check full errors and keyboard focus with enlarged text and VoiceOver/NVDA; retain focus, audio and timing evidence. Offscreen tests cover transitions, persistence, clipboard and delayed window handoff, not physical capture/audio or assistive technology.

## Qualify compact controls over a real game

- [ ] With verified background workspace isolation, or an explicitly approved native session, exercise compact controls on macOS and Windows over a fullscreen game: Start, Stop, Cancel start, Read once, pause/focus loss and expected-line recovery. Confirm capture/audio stop, no focus theft, screen-edge placement, and keyboard access to the complete last speaker, latest message and unavailable reason at enlarged text with VoiceOver/NVDA. Retain native focus, screenshot and audible-stop evidence. Offscreen tests qualify state, dispatch, layout, scrolling/copy and focus indicators, not fullscreen or assistive-technology integration.

## Qualify native local voice import

- [ ] With verified background workspace isolation, or an explicitly approved native session, open the audio file picker by keyboard on macOS and Windows; choose and cancel multiple local recordings, then retry after a file becomes unavailable. Verify focus restoration, enlarged system text and VoiceOver/NVDA announcements for invalid names and replacement confirmation. Enter/Escape must keep the old voice; explicit Replace voice must update only the intended custom character. Use a disposable library and retain native focus/announcement evidence. Offscreen Qt tests and renders qualify draft, keyboard dispatch, scrolling and guarded publication, not native picker placement or screen-reader output.

## Keep full regression shards within their watchdogs

- [ ] Measure and repartition the broad `remainder` inventory in `scripts/run_ci_unittests.py`, including a separate bounded group for catalog renders, while preserving every discovered test and the existing watchdogs. In the macOS permissions UI cycle the 900-second aggregate timed out during catalog renders; excluding the catalog still completed 4,460 of 4,475 remainder tests before the deadline, without an assertion failure. Use exact test-ID union checks and investigate slow cases rather than raising limits or dropping coverage. Validate the normal full-suite entry point on supported CI platforms; locally split retries qualify the tests, not the current aggregate scheduling.

## Qualify native macOS permission recovery

- [ ] In an explicitly approved native macOS session (OS prompts and System Settings may interrupt the desktop), test Screen Recording and Accessibility separately: Request, deny/grant/retry, and Open/Manage Settings. Confirm each opens the corresponding system control; change access and return to VNTTS, then verify automatic status refresh and manual Refresh status. Check pending-request Close/Escape, keyboard focus and VoiceOver names/descriptions at enlarged text; preserve screenshots and focus/announcement evidence. Offscreen tests verify dispatch, state, keyboard, scroll/copy and exact target URLs, not native OS routing, prompts or accessibility announcements.

## Qualify native OCR review and editor accessibility

- [ ] Once native UI can be launched without interrupting the desktop, check saved OCR corrections on macOS and Windows at enlarged system text: put the narrow editor near screen edges, hover both long source/replacement cells and inspect each with F2/Escape. Confirm full values remain readable, hover stays usable, native tooltips remain on screen, and exiting inspection leaves Save disabled. Retain full-window/tooltip captures and keyboard evidence. Offscreen tests and cropped tooltips verify content, not native placement or focus.

- [ ] With verified background workspace isolation or an explicitly approved native session, review a captured uncertain line on macOS and Windows at enlarged system text. Check VoiceOver/NVDA names and status announcements, Tab/arrow access to every footer action, literal-text copying and tooltips near screen edges. Save a scoped correction, reopen after an external rule edit, then Read Once on a real game and confirm the updated correction is used. Retain native focus, capture and accessibility evidence; offscreen tests do not qualify OS routing or assistive technology.

## Qualify dialogue history on native audio and accessibility setups

- [ ] On macOS and Windows with a real speech engine, capture two lines, open Dialogue history, start one line and select/search for another, then stop and retry after restoring an unavailable engine. Confirm the assigned voice, actual audible stop and named status agree; verify Tab/Enter/Escape and full error scrolling/copying at 200% text with VoiceOver/NVDA. Retain native screenshots, audio and focus/announcement evidence. Automated Qt state/keyboard/export tests and Mac window renders do not qualify hardware output or screen-reader/native focus behavior.

## Investigate reported slow-sounding voice playback

- [ ] Identify and retain an actually slowed audible sample from the original/preview/reading paths, then compare its native DAC duration with its PCM frame count and rate before changing playback or synthesis speed. The current Mac reproduction plays Centurion original 12.51 s and MOSS preview 3.84 s at matching DAC durations, with 100% configured speed and no underflow; this does not explain the user's earlier report.

## Active - Faster voice auditions and qualified reference selection

- [ ] **Validate general automatic reference suitability:** build reproducible positive/negative acoustic controls from authorized installed sources and qualify any new output rule against independently accepted examples; preserve variable repeat outcomes as uncertain. Store the fixture recipe and expected decisions in Git; private historical ratings are not a required input. Require evidence on known natural defect types, balanced shared texts/seeds and independent character/source groups before assigning reference-ranking weights; retain an uncertain outcome and verify sample-rate/gain sensitivity. Do not fit thresholds or invert failed metrics from these development labels; injected controls are not natural-defect precision. Further human listening must answer a concrete unresolved question using a few targeted cases and a stated stopping decision, not another exhaustive batch. Keep named references/ratings research-only and ordinary users free of per-character A/B review.
- [ ] **Finish hardware qualification of audition latency/resources:** measure physical audible start and peak/model memory; qualify rapid navigation, close/cancel, explicit unload/reload and switching distinct model weights on Windows/native MOSS and additional supported backends. Record a fresh reproducible Mac/Windows baseline with the chosen model and hardware; historical Mac experiments do not establish hardware-wide certification. The Mac replacement check used two identifiers for identical weights; native model/language restoration and canceled replacement retry still need real hardware checks. Qualify the 300 ms first-ready target across devices, separating remaining native output initialization from cache lookup; retain device-change/error/cleanup checks before considering stream warmup. Do not broaden the speculative window beyond three requests.
- [ ] **Finish reference-selection baseline:** compare the current trio with eligible outside references at the same three-generation budget using complete inventories regenerated from the same installed sources with recorded versions/checksums; keep Sonetto's routed-only counterfactual separate from successful imports. Test source/length diversity as a hypothesis against the current four-second ordering, with shared texts/seeds and all failures retained; do not infer synthesis quality from catalog size or equal technical scores. Create a small reproducible, human-accepted control set for false defect warnings, preserving ordinary intonation/stress variation; record source identity, text/seed and expected judgment in a tracked fixture recipe instead of requiring private WAVs. Qualify independent characters/performers and ownership of unlinked media separately from synthesis quality; the Mrs. Owen preference cannot bypass the identity gate. Check held-out phrases; keep variable repeats, render-block dependence and complete-intersection exclusions explicit. Historical sweeps with different text/runtime are not before/after evidence, and any new listening must resolve a concrete uncertainty with a few targeted cases.
- [ ] **Calibrate useful synthesis-defect recommendations:** optimize assistance for the user's final choice, with visible uncertainty and tolerated errors, rather than requiring an infallible detector. Build tracked normalization/control fixtures that separate writing/boundary variations, ambiguous recognition and possible added/missing content; keep weak ASR evidence from imposing hard reference rejection. Verify practical usefulness independently before assigning calibrated confidence or reference-ranking weights; the user's illustrative 90% is not a measured result. Keep automatic replacement/retry qualification separate from advisory recommendations: preserve the original and establish net audible benefit before enabling replacement. Record cohort limits and do not fit exception lists or thresholds to those same examples. A historical single-seed reproduction found an ASR-exact seed+1 replacement; reproduce from recorded model/text/seed inputs before using that result, and require human acceptance independently of ASR. Padding changes conditioning content/length and is not a qualified preparation fix. Retain content, silence, clipping and perceptual checks as separate signals. Preserve ties and neither, count every attempted render, and do not infer absolute scores from comparative labels or globally blacklist a reference from one bad seed.
- [ ] **Qualify improved ranking:** separate character ownership from backend/conditioning-specific suitability; unsupported speech/music/noise/multiple-speaker/transcript signals remain unknown. Keep ASR and speaker similarity diagnostic until calibrated on independent anchors and held-out characters/performers; do not promote an arbitrary replacement for the four-second heuristic or require expensive qualification for every ordinary preparation.
- [ ] **Refresh older automatic suggestions when policy changes:** distinguish unreviewed legacy automatic bindings from explicit user choices and conditionally migrate only unchanged algorithm-created suggestions for the same character. Preserve concurrent user changes and immutable running/prepared job snapshots; current advisory recommendations intentionally retain existing bindings. Qualify migrations independently of fresh unbound suggestions.

## Repair current GitHub CI failures

- [ ] Qualify native UI test ownership so discarded tray widgets are destroyed on the GUI thread; obtain a reproducible owner/lifetime sequence before changing it. If native app exit0 after initial UI readiness recurs unexpectedly, capture the quit reason and in-flight task/drain order; manual closure versus unexpected exit remains unconfirmed. Keep that exit investigation separate from the reproduced retired-signal completion and from Rust feeding.
- [ ] Qualify the existing interpreter-shutdown watchdog and fixed shard/timeout failure labels on actual Windows CI, including post-test worker hangs. Bounded local subprocess tests do not establish Windows host behavior.
- [ ] Verify all 13 non-audit GitHub jobs on the temporary branch `codex/ci-verification-7f4e78be`. Keep the deferred dependency-audit failure unchanged and delete the temporary remote branch after verification.

## Follow the system audio output during playback

- [ ] Verify hotplug on real macOS and Windows headphones while Reading, sample playback and headless CLI playback are active, including disconnect/reconnect and recovery after no output is available.

## Track transitive authoring publication inputs

- [ ] Expose the consulted-file inventory from `audio_event_workspace.validate_audio_event_composition_state_item`, reached through `generation_state._validate_success_audio_event_composition`, and carry it through failure-corpus publication, so a replaceable corpus cannot overwrite nested workspace/composition/review/base authorities. Current collision guards protect explicitly captured queue/state/manifest and successful state WAV paths; custom state-loader callbacks retain opaque additional I/O. Reuse the validator's admitted inventory instead of maintaining a duplicate path table in the corpus writer.

## Continue regular-file admission through remaining integrity owners

- [ ] On Windows, qualify persistent NPY1/2/3 cache reads with the pinned NumPy internal header parser: malformed declared extents must miss before allocation, valid mono/stereo/Fortran/endian entries must retain conversion, and truncation must close the source without recency changes. Qualify candidate-directory replacement/alias refusal before cleanup, active claims and normal retry with native directory identities/locking/rmtree. Mac proofs do not establish Windows behavior.

- [ ] Qualify admitted SHA readers and direct/recursive/runtime copy behavior on Windows, including native metadata/alternate streams, DLL/junction handling and executable behavior. Local Mac output identities, metadata, relocated interpreter provenance and warm-cache throughput passed; cold-disk/network throughput and native model generation remain unmeasured.

- [ ] On Windows, validate admitted bounded WAV/PCM decoding, PNG/JPEG/TIFF screenshot decoding and RGB/crop after source closure, cancellable MOSS model hashing, optional reference duration and native log diagnostics. Exercise registered and legacy inline MOSS references, unchanged-reference cache reuse, cancellation/retry and decoder/source cleanup using real files; Mac descriptor and loopback tests do not certify Windows virtual I/O or actual model generation.

- [ ] On Windows, qualify complete and streaming admitted text reads: UTF-8/BOM/ASCII/default locale, regular aliases, source closure and JSONL first-line/iteration behavior. Verify the dependency-audit script starts before project installation. Mac FIFO/descriptor proof does not qualify Windows filesystem behavior or non-UTF-8 locale execution.

- [ ] On Windows, qualify admitted ZIP intake for MOSS runtime, managed game decoder and packaged licences, plus native-probe archive member sources. Verify valid/invalid archives, source closure, member bytes/metadata, refusal before publication and retry. Mac FIFO/byte-parity tests do not establish Windows filesystem or DLL behavior.

## Coordinate source-audio producer numeric admission

- [ ] In `reverse1999-extractor`, qualify and fix `SourceAudioDurationProbe.probe` optional decoder ratio overflow: positive unbounded `playSamples=10**400` and `sampleRate=1` raise outside its tolerant exception tuple. Add `OverflowError` to that boundary, test overflowing and finite huge ratios, and release/pin only through the separately authorized cross-repository workflow; the current VNTTS consumer repair does not fix the pinned producer.

## Qualify remaining model dependency risks

- [ ] Rerun `scripts/dependency_audit.py` after explicit permission to send
      dependency versions and locked Git revisions to OSV (`api.osv.dev`).
      The October 9 rerun was rejected before execution by automatic approval
      review; local lock consistency checks do not replace this external audit.
- [ ] Qualify the upstream Transformers custom-generation consent fix in each
      affected model stack before changing locks. Commit
      `cbc1651a032b923da7f4b44b3d0e6f68e6ba6b55` is included in released 5.17.0,
      but GHSA-x9r9-c232-4q39/PYSEC-2026-4174 still lack a fixed-release mapping;
      do not equate source verification with runtime compatibility or add an
      unproven ignore. Retain loaded-module provenance and actual model
      generation/voice checks for any upgrade, backport or replacement.
- [ ] Resolve compatibility before updating root Coqui 0.27.5 (Transformers <5
      and private generation API) or Chatterbox Nano's pinned Git package
      (requires Transformers ==5.2.0). The supported UI/profile/CLI paths do not
      supply remote custom_generate, but root's public Python synthesis kwargs
      can; preserve that API while qualifying a compatible fix.
- [ ] Qualify the fixed candidate against MOSS Delay's trusted remote processor
      and model loading, including AutoModel's injected GenerationMixin and
      automatic custom-generation lookup. The captured default model snapshot
      has no custom_generate files; mutable/default or custom repositories are
      not a permanent reachability exemption. Preserve revision forwarding,
      CUDA/BF16 refusal, dtype/attention options and actual generated audio.
- [ ] Qualify MOSS SoundEffect's pinned pipeline and local text-encoder forward
      path against a compatible fixed Transformers build. Its exact model
      snapshot has no custom_generate files and the normal pipeline does not
      call HF generate; retain pinned source/model provenance, CUDA/BF16 and
      audio-quality checks rather than generalizing to arbitrary custom models.
      Qwen's remaining Accelerate, setuptools, Torch and Transformers findings
      belong to the existing Qwen qualification task below, including this
      custom-generation advisory.

## Validate OpenMOSS progress on Windows

- [ ] The September 29 22:48 support bundle still records `[WinError 5] Access is
      denied` replacing `generation-state.json` during another 568-line job.
      Diagnose the writer/reader or antivirus sharing boundary, then confirm
      sustained progress updates, cancellation and Continue retain completed
      lines without frequent state reads or another replacement failure.

## Validate capture changes during background preparation on Windows

- [ ] While a story is preparing and reading is active, confirm Settings and Calibrate capture remain available. Opening either stops only reading, and story preparation continues; after saving a region, restart reading and confirm capture uses the new area.

## Adopt Qwen for offline story preparation

- [ ] Listen to transcriptless Qwen game-voice samples on Mac and Windows before
      choosing it as the default offline engine. The speaker-only conditioning
      path is functional but may preserve expression less well than a reference
      with verified spoken words.
- [ ] On the RTX 2070 Super 8 GB, generate the fixed character corpus with the packaged Windows Qwen 0.6B runtime. Compare its voices with MOSS, confirm memory use and complete lines, and retain the audio and timings. The Mac Qwen 1.7B comparison has passed listening review but does not establish Windows 0.6B quality.
- [ ] Resolve the Qwen dependency audit findings or explicitly accept their release risk. After the Windows quality check, make Qwen the preferred offline engine on supported installations and verify narrator setup and story preparation; keep MOSS available as an alternative.

## Active - Finish repeated UI scanability rollout

- [ ] Validate the integrated voice-route layout on Windows at normal and enlarged text sizes: wheel and keyboard navigation must reach the last route and Inspect/Edit/Generate actions; clicking or double-clicking a row must select and inspect that character. Check all-or-none portraits when game references are mixed, and retain screenshots.

## Active - Review remaining source-extraction UI

- [ ] In `reverse1999-extractor`, reduce empty Character Story table/portrait space so review and A/B actions are easier to reach, and scale table headers with enlarged text.
- [ ] Re-render Character Story review, resolve Astra findings and risks, then run extractor UI tests and an independent code review. Integrate a released extractor revision into this project's pinned dependency only after the user authorizes the cross-repository workflow.


## Validate game voice provenance on Windows

- [ ] In Stories, choose a different reference through `Use this voice` and confirm the voice plan returns promptly without checking all saved audio. Then start generation and confirm the deferred audio check succeeds; attach a support archive if either step still takes over a second before generation starts.
- [ ] Reopen A Fledgling's Brave Fall on Windows. Confirm Everecho receives a game voice automatically when equally ranked story recordings are available, and genuinely unresolved roles show the review action prominently before generation.
- [ ] Validate the repaired mid-preparation game-index refresh on Windows without deleting the import or choosing Centurion again. After voice discovery updates `story-index.jsonl`, confirm Step 2 resumes with the same story selection and saved narrator. If a selected story disappears, confirm the saved voice remains and Stories asks for a new selection; Cancel during refresh must close the dialog.
- [ ] Open Voice plan and Voices with a fresh installed-game import. Confirm known game dialogue displays its original text, unlinked bank clips are explicitly labelled and available only for manual choice, and the saved reviewed Mrs. Owen clip remains selected after reopening. Old candidate catalogs should refresh without deleting the game import.
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
- [ ] **Diagnose Stage 1 auto-advance stalls on Windows:** the September 29
      archive has 21 `story-line-no-match` routes and seven dispatched keys with
      no confirmed successor. New bounded frame-gate counters now distinguish
      fingerprint resets from stable confirmations without retaining dialogue.
      Reproduce one stall and manual advance in the same stage, classify its
      cursor/OCR/focus transition, including cases where a short phrase is read
      but the next dialogue never advances. Check why a manual skip can
      leave the next line silent or require restarting Reading. Fix the proven
      boundary, not just the timeout; never blindly resend a key, and preserve
      one key per dialogue. Identify which unmatched routes are OCR/nameplate errors.
- [ ] **Validate Stage 1 post-playback skips on Windows:** rerun
      `reverse1999:314501:10` (Nowa Miedź office) and `:71` (gray-and-white
      clothing) after the tracker accepts equivalent OCR spelling and line-wrap
      variants. Confirm each completed playback advances once without extra
      audio; a genuinely changed line must still remain blocked.
- [ ] **Complete remaining native acceptance after OCR-cancellation repair.**
      Repeat prepared reading with background MOSS and selected-window/process-audio
      recording. Verify genuine incomplete phrases wait, and lines7/10/16 complete
      without unsolicited interruption. Inspect `observation-invalidated` and
      `queue-cancelled` with confidence/threshold and playback ownership to identify
      any remaining interruption; uncertain OCR must withhold auto-advance without
      suppressing admitted speech. Retain the already passing short-line15
      Narrator fallback, one-key-per-completion, mid-clip Pause/Resume and Stop/restart
      as regression gates. Run from a cold worker after the capture-arming repair
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
- [ ] **Validate Start/Stop on Windows:** the button now changes to `Cancel start`
      while chapter identification runs, then `Stopping reading...` and disabled
      while capture/speech quiesces; all launch surfaces share the transition.
      Verify a rapid double-click cannot restart reading or duplicate Stop, and
      that returning to Stories during reading leaves preparation progress and
      Cancel usable. Compare `live-stop-interrupt` with `live-stop-quiescence`
      in the next support log to isolate any remaining multi-second delay; the
      earlier archive recorded `_wait_for_live_reader` at 5021 ms. Keep a
      stalled start, and investigate any stop timeout before changing limits.
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
      readiness work at normal and enlarged text sizes. Selecting only an
      unread stage must not regenerate finished stages, and Stage 1 remains
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
- [ ] **Validate on Windows:** resume the supplied 682-line story with the recovery
      fix and confirm that the two short `I ...` lines are repaired while the two
      exhausted MOSS lines become live fallbacks without aborting pack publication. The latest
      state has 580 generated and four failed items: the two Aderyn failures share
      a two-character transformed input and were rejected only for 0.64 seconds of
      trailing silence in a 1.12-second WAV; Poacher I hit the 8.5-second limit and
      Poacher II hit the 3-second limit. Trim or tolerate safe edge silence for the
      short utterances and publish explicit live fallbacks for exhausted limits.

## P0 - Validate the non-blocking Voice plan

- [ ] **Validate changed-story edge cases on Windows:** if a previously selected
      story disappears after a game update, the saved Centurion choice must remain
      while Stories requests a new selection. Cancel during content refresh must
      close the dialog. The normal same-story refresh already passed player review.
- [ ] **Validate Mrs. Owen on fresh Windows state:** after updating the extractor,
      open her voice from both Voices and Stories. Both must expose the same
      checksum-bound 3.17-second media `562400954` and 1.95-second media
      `599773947`, without duplicate quoted role labels. Play the original,
      generate a preview, save the 3.17-second candidate, reopen, and prepare a
      story. Gate: the saved choice remains selected and offline generation uses
      that exact reference; no automatic choice silently replaces it.
- [ ] **Validate Rhiannon's playable references on Windows:** with extractor
      commit `2be370e` installed, run a fresh voice import. Confirm her main-bank
      9.0-second line appears among the three Voice plan options alongside story
      lines, while Greeting (15.3 s) and Chitchat I (14.9 s) remain visible only
      for manual choice in Voices and Story inspection. Play the correct originals,
      generate previews, and verify a manual choice survives save/reopen. The
      previous schema-v2 report cache must refresh; reject clips with other
      technical defects. Keep the 45.8-second Chitchat II optional, not a default.
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

## P1 - Link character identities across story names

- [ ] **Validate linked names on Windows:** in a story showing only Aderyn, type
      Rhiannon as the canonical name in Voice plan. Inspect the shared reference
      choices and selected voice, prepare a story, restart and read both names
      with the same voice. A saved short Aderyn clip must remain available for
      inspection but not become the active choice. With only that clip available,
      require preview rather than selecting it automatically. Unlink and verify
      prior choices can be recovered without a fresh game import.
- [ ] **Validate game-evidence name suggestions in the real Voice plan:** select
      The You That's Meant To Be and inspect Aderyn. The only proposed match
      should be Rhiannon, based on five shared portrait IDs and the same voiced
      source bank; unrelated names must not be suggested. Cancel the link dialog
      and confirm no voice changes, then confirm the link, restart and unlink it.
      Gate: no automatic identity or voice change before confirmation, generation
      never waits for a decision, and the prior separate choices remain recoverable.

## P1 - Reduce preparation and post-generation saving latency

- [ ] **Validate finalization on Windows:** repeat the 1,071-file story that took
      73.8 seconds after recovery (acceptance 33.14, publication 38.09,
      activation 2.29). Inspect the new `pregeneration-acceptance-*` and existing
      `pregeneration-publication-*` phases from the last generated line to usable
      audio. The local 1,090-WAV copy took 7.65 seconds cold and 0.65 seconds on
      unchanged reuse; its longest phases were terminal validation (3.48 seconds),
      staged validation (1.92 seconds) and audio copy (1.47 seconds). Require the
      Windows result to preserve published bytes and corruption failures; if a
      phase remains materially slower, collect its file counts and bytes before
      changing its validation boundary.

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
- [ ] **Validate combined PSM 6 OCR on Windows:** with an installed or bundled
      Tesseract, run the two sample-image parity/process-count tests and one live
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
      In Settings, reach the final Advanced speech field and restart note by
      keyboard at 200% scaling; focus must scroll into view while Save stays
      visible. Confirm Cancel discards a cross-section draft and Save persists it.
- [ ] **Validate story-match recovery on macOS and Windows:** with an unmatched
      visible line and the smallest supported display at 200% text scaling, use
      VoiceOver/Narrator and keyboard only. Require the mismatch to be announced,
      all three actions and their focus to remain visible and distinguishable,
      Enter to start live OCR reading where supported or retry matching in sequence
      modes, Stories to open with reading stopped, and Escape to leave reading
      stopped. Confirm emergency stop dismisses recovery and a settings restart
      invalidates the old choice. Retain an active-window/full-display recording
      or accessibility trace, including visible primary-button focus.
- [ ] **Validate Readiness on macOS and Windows:** at 200% text scaling, use
      VoiceOver/Narrator and keyboard only through loading, an OCR error, a voice
      warning and success. Require the first error and its remedy to be announced,
      Full check details and its text area to have visible keyboard focus,
      the full long error to scroll to its end, the install guide to open the
      official Tesseract page, cancel/retry to show no stale result, and Open
      Voices/Open Reading to return focus to the correct destination. Retain a
      short recording or accessibility trace.
- [ ] **Validate macOS permission recovery on a real host:** deny and grant Screen
      Recording and Accessibility independently, return from each System Settings
      pane, and verify automatic status refresh. Check the unknown-status and
      request-failure paths, restart after a newly granted permission, then
      confirm Reading can capture text. At 200% text scaling, both paths and
      Refresh/Close must remain keyboard and VoiceOver accessible.
- [ ] **Validate the asset manager on real macOS and Windows hosts:** at 200% text
      scaling in light and dark themes, use keyboard and VoiceOver/Narrator to
      choose, checksum-validate and save an existing voice manifest; import a
      manifest and one character voice; verify, download, cancel and retry an
      XTTS model. Confirm changed model selections never inherit another
      model's ready state and that failure text stays readable. Verify complete
      error/path copying, unsaved selection cancellation, imported-file permanence
      after Cancel, and close during import. Automated Qt checks and native macOS
      library validation cover these states without qualifying assistive technology
      or real network transfers.
- [ ] **Validate game profiles on real macOS and Windows hosts:** at 200% text
      scaling, reach the full selected-settings summary by keyboard scrolling,
      distinguish same-prefix profile names in the open selector, and confirm
      every action remains visible. Keyboard focus must follow selection/management/activation order;
      unavailable actions are announced as disabled, and Close does not imply
      rollback of saved profile edits. Simulate a read-only region file and full
      settings volume to verify activation leaves the previous profile usable.
- [ ] **Validate manual voice import on real macOS and Windows hosts:** use keyboard
      and VoiceOver/Narrator to enter required character/optional aliases, select
      and replace audio files, inspect a long scrollable file list, cancel without
      import, and confirm an import error preserves the entered selection for retry.
- [ ] **Validate calibration on real macOS and Windows hosts:** at 200% text
      scaling with VoiceOver/Narrator, select one complete dialogue box (name,
      multiline text and continue indicator), move/resize it, review and enlarge
      the capture, scroll to its edges, and cancel without changing prior settings.
      Include near-fullscreen/top-positioned dialogue and real game fonts. Deny
      region-file write permission, then restore it and retry. Confirm only the
      active profile (or global region without a profile) changes on Save and old
      manual OCR subregions clear only after successful saving. Retain native
      screenshots, focus/announcement evidence and before/after config values.
- [ ] **Validate uncertain OCR review on real macOS and Windows hosts:** at 200%
      text scaling, correct one speaker-only and one text-only sample, inspect a
      wide/tall screenshot with both scrollbars, retry after read-only storage,
      and confirm the rule reaches only its selected game scope. Retain screenshots
      of compact, error and zoom states plus the saved correction entries.
- [ ] **Validate OCR correction editor on real macOS and Windows hosts:** at 200%
      text scaling, add/edit/delete rules in both scopes, hover and keyboard-edit
      a long clipped value, cancel an uncommitted cell edit, inspect first-error
      tab navigation, and retry after a denied write. Keep screenshots of compact,
      validation and discard-confirmation states plus the persisted rule file.
- [ ] **Validate dialogue history on real macOS and Windows hosts:** use keyboard
      and a screen reader to search, select, speak and stop a line; verify that
      current voice assignments are used, status changes are announced, the full
      session exports even under a filter, and the compact layout works at 200%
      text scaling. Confirm a backend without Stop keeps the window open until
      speech finishes.
- [ ] **Validate diagnostics and support on real macOS and Windows hosts:** at
      200% text scaling, inspect the capture summary, expand separate speaker and
      dialogue OCR images/confidence, and enlarge each image with both scroll
      axes. Trigger permission/window warnings and a timeout; verify stale data
      stays dated, full warnings can be scrolled/copied, and Refresh and its remedy
      remain keyboard/screen-reader reachable. From one successful native capture,
      confirm the original, speaker OCR and dialogue OCR enlargements belong to
      that same snapshot, then retry after timeout and restored permissions.
      Browse a growing log without losing selection, export and
      inspect a support archive for screenshots/dialogue/audio/secrets, then
      retry after a read-only destination. Retain screenshots and archive inventory.
- [ ] **Validate the authoring workbench on real macOS and Windows hosts:** at
      200% text scaling, choose collections with and without pending lines,
      generate one line, stop playback before the end and confirm Approve/Reject
      stay blocked, then finish playback and approve. Reopen and replay the
      approved recording, including while another window owns generation;
      confirm review decisions wait for the lease, saved review filters remain
      independent of collection selection, and the full selected dialogue text
      stays visible and keyboard/screen-reader accessible. Change collections
      during reload and confirm Generate/Retry wait for the new scope. Stop a
      real generation process, including Stop and close, and confirm the window
      closes after termination. Trigger a workspace load failure, scroll/copy its
      complete literal diagnostic, restore the input and Retry; confirm stale
      counts disappear and recovered controls announce their current state.
      Retain screenshots, native focus/announcement and playback/decision evidence.
- [ ] **Validate cohort review on real macOS and Windows hosts:** at 200% text
      scaling, listen to every required sample to the end, mark one unclear
      defect and replace it with a specific reason, accept a clean cohort,
      reject a bad cohort, request more evidence when available, and leave
      undecided. Trigger a bundle-load failure and verify the blocked status and
      Retry are visible without scrolling; confirm keyboard/screen-reader order
      and that checksum or stale-authority errors cannot commit a decision.
      Stop while a recording is preparing and switch samples during playback;
      confirm no delayed sound or false heard mark. Close during loading/saving
      and verify completion, then force a listening-progress write failure and
      retry or explicitly discard. Retain native audio-stop, focus and
      VoiceOver/NVDA evidence; offscreen callbacks do not qualify physical output.
- [ ] **Validate blind missing-voice review on real macOS and Windows hosts:**
      at 200% text scaling, compare two playable A/B candidates, stop each
      before the end and confirm Neither/Choose remain blocked, then hear both
      fully and save one choice. Repeat with one failed candidate and with both
      failed; require the read-only automatic unresolved summary, keyboard and
      screen-reader access, and no disclosure of hidden source identity before
      import. Confirm a mixed multi-family session counts manual and automatic
      outcomes separately after reopen.
- [ ] **Validate failed-reference audit on real macOS and Windows hosts:**
      at 200% text scaling, use keyboard and VoiceOver/NVDA to switch groups,
      candidates and affected lines in both directions; confirm the full line
      remains readable and focus scrolls into view. Interrupt original playback,
      switch selection during playback, fail source preparation and generate or
      cancel a preview. Only full original playback of every candidate may enable
      either decision; a preview must never count as original evidence. Save and
      reopen to confirm the selected reference and progress persist.
- [ ] **Validate source-reference quality review on real macOS and Windows hosts:**
      at 200% text scaling, inspect the source identity/portrait, switch generated
      samples, copy their full text and use keyboard plus VoiceOver/NVDA to reach
      playback and decisions. Stop original and generated audio early and switch
      samples mid-playback; neither action may count as complete listening. After
      the original alone, Reject and Need another should enable while Accept
      remains blocked; after every generated sample, Accept should enable.
      Save and reopen a final decision and confirm the completion message stays
      visible at enlarged text.
- [ ] **Validate blind A/B listening on real macOS and Windows hosts:** at
      200% text scaling, read and scroll a long dialogue, use keyboard and
      VoiceOver/NVDA to reach A, B, Review context and all four decisions.
      Confirm a pause/resume preserves only natural full-playback credit, seeking
      or stopping does not grant credit, and automatic playback starts B only
      after A finishes. Save A/B/tie/neither outcomes; verify no model is called
      a leader for tied or all-neither results, the report button receives focus,
      and the saved report opens in the default application.
- [ ] **Validate terminal-conflict review on real macOS and Windows hosts:**
      at 200% text scaling, read a multiline affected line, move by keyboard and
      VoiceOver/NVDA through Play A/B, Review context, and all three outcomes.
      Stop playback early and verify no listening credit; natural completion of
      both recordings alone must unlock Keep A/B and Neither. Save a decision,
      reopen, and verify progress and unchanged source workspaces. A dangling
      progress artifact must block opening rather than present a fresh review.
- [ ] **Validate rejected-recording reassessment on real macOS and Windows hosts:**
      at 200% text scaling, hear two recordings to their natural ends, stop one
      early, choose multiple defects and then acceptable, navigate both ways,
      and publish. Check VoiceOver/NVDA announces the evidence, choices, save
      status and errors; confirm focus follows the scrolled final defect.
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
      SoundEffect v2 using a small fixed corpus and multiple checksum-bound seeds.
      Require technical and blinded perceptual approval before adding a provider;
      unsupported effects remain explicit omissions.
## Active - Voice-picker accessibility verification

- [ ] On supported macOS and Windows desktops, exercise the selected-story voice
      overview at minimum width and 200% text with keyboard and VoiceOver/NVDA.
      Traverse the table, selected reference details, More actions and Preparation
      details; require full values/actions to be reachable, the selected group to
      remain announced, and Copy to preserve complete details. Choose a voice through
      a canonical character and through a linked name; require one shared choice
      in other stories, the correct voice in audition, no assignment selector and
      reachable clear/unlink actions. Existing prepared audio must stay unchanged.
      Change to an imported
      narrator and listen before Apply voice changes; require the current narrator's
      original recording. Apply the changes before opening character audition. Retain native screenshots and a screen-reader/audio trace.
- [ ] Diagnose the Windows Mrs. Owen original-reference silence. Record the
      selected reference checksum and authoring playback queued/started/finished/
      failed states in support bundles without recording audio or dialogue. The
      reported failure was a mismatch between the candidate's source display name
      and its manifest name; verify original playback and preview on Windows after
      the identity-check fix, then remove this item.
- [ ] On supported macOS and Windows builds, use VoiceOver and NVDA to traverse
      target, source, game-reference, consent, preview and action controls;
      enter the same editor from live character recovery with that character selected.
      Confirm an unknown character in live and Read once uses the narrator without
      opening a blocking choice. After a live voice edit, exercise Save, Back,
      Resume with narrator and Escape; Stop or a failed/cancelled voice reload must
      never restart reading. Exercise the same optional edit after Read once and
      require Cancel without a paused-recovery prompt.
      Trigger original and preview playback states. Gate: focus follows visual order,
      every control has a meaningful announced name, and each changing status is
      announced once with its subject. Retain a screen-reader transcript or recording.
- [ ] In Preparation voice audition, use a source with a known title and transcript;
      require ordinary evidence to remain visible outside technical details. Exercise
      in-place Play/Stop for original and preview, switch candidates during playback,
      and inject a delayed save failure while pressing Back. Gate: stale audio stops,
      Back waits for persistence, Retry saves the same visible choice, and the replanned
      route matches that choice. Repeat the flow by keyboard and screen reader.
      At minimum width and 200% text, open both source and phrase dropdowns near
      the display edges; require visible options and a keyboard return to the
      selected control. Browse all 18 references, compare Ready with speech-check
      warnings, copy a long status/error, and verify Play/Stop labels and control
      positions at enlarged text. Retain real desktop screenshots on both OSes; virtual Qt
      geometry alone does not establish native screen-edge behavior.

## Active - Offline preparation hardware acceptance

- [ ] On supported macOS and Windows model runtimes, start one-shot and live
      Reading while a speculative reference preview is queued and while it is
      rendering. Require cancellation to be requested before reading starts,
      queued work to be removed, no background restart during startup, speech,
      pause or stopping, and eventual model release without blocking the window.
      Retain runtime cancellation/timing logs and audible playback evidence;
      coordinator regression tests use a controlled renderer, not the real model.

M1 owns concurrent chapter-generation acceptance, including deferred activation,
restart and completed-line reuse, on Mac. Windows platform acceptance remains a
separate future gate.

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

## Qualify packaged preview ASR isolation

- [ ] Before shipping frozen macOS/Windows builds, smoke-test preview ASR spawn from the packaged entrypoint: start checking during playback, play ready audio during checking, cancel/close and reopen. Confirm no second desktop window, no surviving ASR worker and continued prefetch. Source-mode Mac qualification and early-dispatch unit tests do not establish bundled behavior; record entrypoint/build, worker PID lifecycle and timing for this packaged run.

## Voices tab - remaining fixes from the October 24 native UI audit

- [ ] **P1 - Add a compact saved-voice overview.** Show a compact character/current-voice overview in Voices and a Play current voice action; distinguish saved binding, auditioned candidate and prepared story recordings. Gate: game/imported/built-in/narrator-fallback/automatic/missing-source cases have useful consistent labels; browsing/cancel never changes the saved-voice display or assignment.

- [ ] **P2 - Qualify selective regeneration.** The separate selective-regeneration audit must qualify that changing one character preserves other characters' ready recordings, including partly generated stories, and that the Stories preparation UI communicates this scope.

- [ ] **P2 - Make saved assignment changes recoverable.** Label saving with the target character and global scope, and provide an undo path for an already saved choice. Gate: undo restores that character's previous binding without changing other characters or prepared audio; deliberate keyboard save and cancellation remain accessible. Use copied data for verification.

- [ ] **P2 - Clarify copied diagnostics.** Check that copied diagnostics include the actual inner foreground preview failure as well as source discovery and background preparation failures. Gate: saved/candidate voices can be identified and auditioned without opening diagnostics; refresh/import remain discoverable with clear side effects and cancellation behavior.

## Saved voice reference metadata

- [ ] Preserve source transcripts/excerpts when saving game references into VoiceLibrary and recover them for old bindings only when the exact audio/source can be verified. A real saved Narrator/Centurion binding has audio bytes and donor provenance but no transcript: the editor can play it, while original text is unavailable and Use game phrase is disabled. Gate: saved references keep their original phrase after reopen, missing historical text stays explicit, no guessed transcript or assignment changes.

## MCP Unicode text input

- [ ] Fix the PySide6 MCP `type_text` input path for Unicode. Native macOS reproduction: focus a QPlainTextEdit and call `type_text` with Cyrillic; QtTest aborts the application at `testlib/qasciikey.cpp:192` (`ASSERT: "false"`). Use Unicode-capable Qt text/input events for non-ASCII instead of ASCII-only simulated keys, preserving cursor/selection and textChanged behavior. Gate: Cyrillic, accented Latin and mixed text append/replace correctly without aborting; ordinary ASCII shortcuts still work. Isolated UI fixtures only; this failure is in MCP input simulation, not evidence of normal keyboard input failing.

## Qualify game-phrase preview silence rejection

- [ ] Reproduce the real MOSS local v1.5 MLX Centurion preview rejection: choose Narrator > Game character voice > Centurion, first 12.5 s reference beginning "Morning ... Huh?", then Use game phrase for "Ooh, it's you, Jefe? Wanna go on an adventure? There are over 261.6 places that have been rated as dangerous. Murder scenes or cliffs, it's your call." The generated WAV failed objective preflight with 1.60 s internal silence. Preserve model/text/reference/seed and inspect audio before deciding whether synthesis failed or the pause gate rejected an acceptable pause; do not loosen the gate from one example. Require a useful retry outcome and visible reason, with unchanged saved voice assignments.

## Remaining - Shared audio storage

- [ ] Qualify shared WAV hard links on the supported Windows filesystem and in
      packaged builds. Keep explicit same-filesystem/link errors and existing audio
      intact; do not add a silent copy fallback. Controlled Mac UI runs and unit
      tests do not qualify these platforms or neural-model/hardware behavior.
- [ ] Retain the legacy NPY reader until proven sample-rate/model provenance is recovered for
      the 16 remaining local synthesis arrays (5 Pocket, 2 MOSS live, 9 offline).
      Keep those originals recoverable; migrate and verify restart lookup before
      removing the parser and compatibility tests. The 542 proven arrays are
      already migrated; other machines still require their own explicit inventory.

## Restore pre-existing repository quality gates

- [ ] Fix the existing mypy errors in `vntts/diagnostic_recording.py`, `vntts/reading_diagnostics_analysis.py` and the preview backend factory contract in `vntts/app.py`; format the existing `vntts/moss_runtime.py` signature. These failures also reproduce on main before the unified-editor integration. Gate: the full configured mypy and Ruff format checks pass without suppressing diagnostics.
- [ ] Bring the existing Reading, recording, generation and saved-audio complexity findings back within `tests/fixtures/ruff-complexity-v2.json`. Run `scripts/check_ruff_complexity.py` for the current scope inventory; refactor coherent responsibilities without increasing allowances, preserve lifecycle/error handling and run affected tests. Gate: the full complexity ratchet passes, with obsolete allowances removed or reduced.

## Remaining - Legacy audio metadata import

- [ ] Retain JSON readers for old synthesis bindings, jobs, selections, voice
      plans/decisions, generation inputs, offline generation state and story
      catalog caches until the supported upgrade window excludes JSON-only app
      versions and inventories
      show no unimported metadata. Keep unknown/corrupt originals recoverable;
      remove these readers and their compatibility tests together after that gate.
      Portable authoring proof snapshots and published pack/queue/voice manifests
      remain JSON contracts and are outside this removal.
