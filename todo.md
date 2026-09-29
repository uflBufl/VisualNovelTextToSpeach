# TODO

## Active - Asset import correctness

- [ ] Apply the supported-audio-format check to imported voice packs as well as individual voices. Gate: unsupported reference regression is rejected, valid `.ogg` import still passes, and changed tests and static checks pass.

## Validate OpenMOSS progress on Windows

- [ ] During a sustained offline story preparation, confirm progress updates without frequent reads of `generation-state.json`, cancellation and Continue retain completed lines, and no state replacement access error recurs.

## Validate capture changes during background preparation on Windows

- [ ] While a story is preparing and reading is active, confirm Settings and Calibrate capture remain available. Opening either stops only reading, and story preparation continues; after saving a region, restart reading and confirm capture uses the new area.

## Active - Qwen voice cloning on Windows

- [ ] Resolve the official `qwen-tts==0.1.1` dependency audit findings without silently suppressing known vulnerabilities.
- [ ] Render and time one Matilda reference on the RTX 2070 Super 8 GB. Check that the UI reports CUDA and the correct GPU.

## Active - Finish repeated UI scanability rollout

- [ ] Revisit the current story tree: story title, readiness, line count, and diagnostics are still concatenated in one cell. Separate primary identity from state and detail without overwriting the in-progress grouping work.
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

## P0 - Validate automatic game-update refresh on Windows

- [ ] After a real Reverse: 1999 update, open Stories without using Add or import
      content. Confirm that the update is detected, the cancellable import has a
      visible status, new stories appear in the existing Game content source,
      previously selected stories and voice choices remain, and an unchanged
      second opening starts no import. If the import fails, confirm the old story
      catalog remains usable and the import can be retried. Check that obsolete
      story-candidate caches are reclaimed without touching saved jobs or narrator
      references.

## P0 - Play while offline audio is still preparing

- [ ] **Diagnose Stage 1 auto-advance stalls on Windows:** the September 29
      archive has 21 `story-line-no-match` routes and seven dispatched keys with
      no confirmed successor. New bounded frame-gate counters now distinguish
      fingerprint resets from stable confirmations without retaining dialogue.
      Reproduce one stall and manual advance in the same stage, classify its
      cursor/OCR/focus transition, and fix the proven boundary. Never blindly
      resend a key; preserve one key per dialogue. Identify which unmatched
      routes are OCR/nameplate errors.
- [ ] **Validate unknown speaker OCR in the affected stage:** the isolated `22`
      nameplate alias now resolves to `???` and is removed from dialogue, while
      legitimate numeric names and numeral-led narration remain intact. Capture
      real frames to verify this alias and identify any other OCR variants;
      inspect unstable nameplate pixels separately if the visual-successor gate
      still resets to one candidate frame.
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
      synthetic overlap, one partial cue still uses TTS, and identify the
      indexed media for `We should all stay on our island.` before claiming the
      reported overlap is fixed. Check that cancelling the one-time model
      download takes effect after its current file and never blocks a retry.
- [ ] **Validate sequence-led prepared playback on Windows:** safe linear stages
      now acquire a checksum-bound raw-step plan in the background; runtime and
      published packs use audio-auto only with the plan and enabled auto-advance.
      Branches, gaps, missing bundles and incomplete cumulative coverage fall
      back to OCR. In stage `314501`, confirm the 107-step plan is acquired,
      known WAVs start promptly, no wrong speaker/duplicate/unsolicited key occurs,
      and manual resync still works. Compare capture/OCR/first-PCM timings with
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
- [ ] **Validate after the live fixes:** verify with one selected multi-chapter
      story: start after a partial chapter, continue generation while prepared
      WAVs play, cross a newly published boundary without duplicate speech, wait
      safely when catching the worker, resume after success, survive
      cancellation/restart, and finish with the same validated pack as
      uninterrupted offline preparation.

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

- [ ] **Validate repeated Voice plan in the real UI:** on the local 253 MB reference
      index, repeated role selection fell from 9.18 to 0.37 seconds. The plan-store
      fixture now produces identical groups while reducing library reads from
      3646 to 214; warm creation took about 0.25 seconds. On an installed game,
      capture the candidate-preparation/store phase logs for first open and reopen;
      require the warm phases to explain the whole wait and saved choices to remain
      identical. Investigate any remaining phase above one second before changing it.
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
      Enter to start live reading, Stories to open with reading stopped, and Escape
      to leave reading stopped. Retain a short recording or accessibility trace.
- [ ] **Validate Readiness on macOS and Windows:** at 200% text scaling, use
      VoiceOver/Narrator and keyboard only through loading, an OCR error, a voice
      warning and success. Require the first error and its remedy to be announced,
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
      model's ready state and that failure text stays readable.
- [ ] **Validate game profiles on real macOS and Windows hosts:** at 200% text
      scaling, confirm the full selected-settings summary and every action remain
      visible, keyboard focus follows selection/management/activation order,
      unavailable actions are announced as disabled, and Close does not imply
      rollback of saved profile edits. Simulate a read-only region file and full
      settings volume to verify activation leaves the previous profile usable.
- [ ] **Validate manual voice import on real macOS and Windows hosts:** use keyboard
      and VoiceOver/Narrator to enter required character/optional aliases, select
      and replace audio files, inspect a long scrollable file list, cancel without
      import, and confirm an import error preserves the entered selection for retry.
- [ ] **Validate calibration on real macOS and Windows hosts:** at 200% text
      scaling, use mouse and keyboard to draw, move, resize, clear and review a
      region. Deny region-file write permission and confirm the selected pixels,
      error and retry remain visible; retry after restoring permission. Check that
      an active profile and global region agree after each success/failure.
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
      200% text scaling, inspect and enlarge a current capture with both scroll
      axes, trigger permission/window warnings and a timeout, and verify stale
      data is dated while Refresh and its remedy stay keyboard/screen-reader
      reachable. Browse a growing log without losing selection, export and
      inspect a support archive for screenshots/dialogue/audio/secrets, then
      retry after a read-only destination. Retain screenshots and archive inventory.
- [ ] **Validate the authoring workbench on real macOS and Windows hosts:** at
      200% text scaling, choose collections with and without pending lines,
      generate one line, stop playback before the end and confirm Approve/Reject
      stay blocked, then finish playback and approve. Reopen and replay the
      approved recording, including while another window owns generation;
      confirm review decisions wait for the lease, saved review filters remain
      independent of collection selection, and the full selected dialogue text
      stays visible and keyboard/screen-reader accessible. Retain the screenshots
      and playback/decision evidence.
- [ ] **Validate cohort review on real macOS and Windows hosts:** at 200% text
      scaling, listen to every required sample to the end, mark one unclear
      defect and replace it with a specific reason, accept a clean cohort,
      reject a bad cohort, request more evidence when available, and leave
      undecided. Trigger a bundle-load failure and verify the blocked status and
      Retry are visible without scrolling; confirm keyboard/screen-reader order
      and that checksum or stale-authority errors cannot commit a decision.
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

- [ ] Diagnose the Windows Mrs. Owen original-reference silence. Record the
      selected reference checksum and authoring playback queued/started/finished/
      failed states in support bundles without recording audio or dialogue. The
      reported failure was a mismatch between the candidate's source display name
      and its manifest name; verify original playback and preview on Windows after
      the identity-check fix, then remove this item.
- [ ] On supported macOS and Windows builds, use VoiceOver and NVDA to traverse
      target, source, game-reference, consent, preview, impact and action controls;
      enter the same editor from live character recovery with that character selected.
      Trigger original and preview playback states. Gate: focus follows visual order,
      every control has a meaningful announced name, and each changing status is
      announced once with its subject. Retain a screen-reader transcript or recording.
- [ ] In Preparation voice audition, use a source with a known title and transcript;
      require ordinary evidence to remain visible outside technical details. Exercise
      in-place Play/Stop for original and preview, switch candidates during playback,
      and inject a delayed save failure while pressing Back. Gate: stale audio stops,
      Back waits for persistence, Retry saves the same visible choice, and the replanned
      route matches that choice. Repeat the flow by keyboard and screen reader.

## Active - Offline preparation hardware acceptance

- [ ] On supported macOS and Windows hardware, start Reading while generation is
      incomplete, cross a prepared boundary, cancel and resume after restart, and
      exercise deferred activation after settings change. Verify audio continuity,
      that completed lines are not regenerated, keyboard and screen-reader status,
      and the minimum-width layout with increased text scaling.

## Investigate intermittent macOS Qt test crash

- [ ] A macOS changed-test run intermittently terminated with `shiboken6` `mainThreadDeletionHandler` SIGSEGV during pure-Python `test_player_session` after UI tests. The exact final 10-test sequence passed 10 times, the full onboarding/person/player sequence passed five times, and a later 2734-test selector passed. Reproduce and bisect an earlier Qt owner in the remainder shard with faulthandler before changing cleanup; retain the crash log in `.codex/investigations/` if it becomes repeatable. Gate: a narrowed failing sequence and a deterministic cleanup check.
