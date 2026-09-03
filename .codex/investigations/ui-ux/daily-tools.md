# Daily-tools UI/UX evidence report

## Scope, method, and confidence

Reviewed source, entry points, and tests for:

- `OfflineAudioPreparationDialog` and embedded `VoiceAuditionPanel`;
- `VoicePreviewDialog` in both general narrator/character management and
  unknown-speaker recovery;
- `OCRReviewDialog` and `OCRCorrectionsDialog`;
- `DialogueHistoryDialog`, `DiagnosticsDialog`, and `SupportCenterDialog`;
- related tray/dashboard actions, live-voice prompts, file choosers, warning
  dialogs, close/cancel paths, and export launchers.

Evidence tags used below:

- **V**: verified directly in source, a passing test, or offscreen runtime
  introspection;
- **H**: heuristic UX judgment that still needs observation/usability evidence;
- **G**: coverage or evidence gap, not a claim that behavior is broken.

Commands run on 2026-09-03:

```text
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -q \
  tests.test_voice_preview tests.test_ocr_review tests.test_ocr_corrections \
  tests.test_history tests.test_diagnostics tests.test_support_ui
# 53 tests passed

QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -q \
  tests.test_pregeneration_setup tests.test_pregeneration_audition_ui \
  tests.test_self_service_pregeneration
# 30 tests passed

QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -q tests.test_app
# 105 tests passed
```

I also instantiated every dialog offscreen, recursively enumerated interactive
Qt children, recorded visibility/enabled/focus/accessibility state, and measured
minimum size hints at 13, 20, and 26 point fonts. This is truthful layout
evidence, but not a substitute for macOS/Windows screenshots or a screen-reader
run.

No UI implementation was changed.

## Surface coverage

| ID | Surface/state covered | Entry/exit | Verified states | Remaining evidence |
| --- | --- | --- | --- | --- |
| DT-01 | Offline preparation selection | Dashboard `Prepare offline audio...`; tray action; modal Continue/Cancel | empty content, discovered content, restored selection, import, planning, audition, generation, recovery, acceptance, publication, cancellation, write/error recovery | Real long story names, native importer availability messages, 150%/200% screenshot |
| DT-02 | Embedded voice audition | Appears within DT-01 only when plan has ambiguous voices; parent Cancel owns exit | loading, two usable candidates, one/zero usable candidates, anchor, alternate phrase, narrator fallback, auto-all, save failure/retry, cancel/prefetch | Keyboard-only and screen-reader pass; long localized text |
| DT-03 | General voice management | Dashboard `Narrator voice`; tray `Choose narrator voice...` and `Manage character voices...`; modal Close | narrator and character modes, preview success/failure/stop/deferred close, assign, clear routing, force-live | Real backend latency and OS audio interruption; large-font screenshots |
| DT-04 | Unknown-speaker prompt + mapping | Runtime floating warning: Choose voice/Continue with narrator; opens DT-03 | both decisions, live pause/resume, cancelled mapping returns to prompt | Fullscreen/multi-monitor and keyboard focus evidence |
| DT-05 | Live-scope voice preflight | Runtime floating warning: Assign voices/Use narrator for all/Cancel | stale approval, changed scope, close, cancellation, assign and retry | Fullscreen/multi-monitor and screen-reader evidence |
| DT-06 | Uncertain OCR review | Tray action; modal Close | populated/empty, save correction, no-op warning, two-step resolve, async write failure/retry/deferred close | Resize-after-image, keyboard-only, large-font, corrupted image screenshot |
| DT-07 | OCR correction editor | Tray action; modal Save/Cancel | both scope tabs, add/remove, validation, shortcuts, async save failure/retry, unsaved-discard guard | Screen reader, long rule sets, 150%/200% screenshot |
| DT-08 | Dialogue history | Tray action; live capture stops before modal and resumes after Close | search, populated/empty, refresh while selection retained, replay/stop/failure, export, deferred close | Keyboard task path, large data/long text, native chooser screenshots |
| DT-09 | Live diagnostics | Tray action or Support launcher; nonmodal Close | empty/latest snapshot, preview, latency fields, warning, conceal/restore, refresh success/failure/timeout at dialog level | Integrated conceal + timeout, large font, small screen, stale image/warning tests |
| DT-10 | Diagnostics and logs | Dashboard `Support and logs`; tray action; nonmodal Close | empty/rotated/appended log, manual selection retained, launcher success/failure, export cancel/failure/success | Keyboard traversal across both tabs, 150%/200%, actual native folder/export failures |

## Explicit control accounting

Disposition vocabulary matches the plan. "Keep" means the control has a real
mission; it does not mean its current accessibility/layout is complete.

### DT-01: Offline audio selection and preparation

Source: `vntts/pregeneration_ui.py:128-227`, state transitions at
`vntts/pregeneration_ui.py:238-847`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| `Game content` combo | First row, after label | Selects one discovered source; preserves selection by story-index digest on refresh. **V** | Keep; give label a buddy and a visible empty/disabled reason |
| `Refresh` | Same crowded source row | Re-runs bounded discovery and updates inline status. **V** | Keep, but merge visually with source selector rather than four equal-weight actions |
| `Choose extracted content...` | Same row | Native JSONL chooser; validates selected story index inline; cancellation is silent. **V** | Keep as secondary recovery action; remember useful start directory if evidence shows repeated manual use |
| `Import installed Reverse: 1999` | Same row; disabled if importer unavailable | Starts background automatic import, changes Cancel to `Cancel import`, and reports terminal state inline. **V** | Keep as primary source-recovery action when available; disabled reason must not live only in tooltip |
| `Choose game folder...` | Same row; disabled with importer | Native directory chooser followed by the same importer. **V** | Merge under/import beside automatic import; it is an alternate input to one operation, not a peer primary action |
| Dynamic story/chapter check items | Main list | Each item names title, total lines, and generation lines; first use checks all, resume restores prior selection. **V** | Keep; list label needs a buddy/accessibility relation |
| `Select all` | Below story list | Bulk checks all visible entries and refreshes estimate. **V** | Keep |
| `Select none` | Beside Select all | Bulk clears selection and disables Continue. **V** | Keep |
| `Change saved voice choices` | Same row as selection bulk actions | Forces existing ambiguous voice decisions to reopen. **V** | Move to an advanced/review-choices disclosure near the voice estimate; current placement groups unlike actions **H** |
| `Continue` | Bottom button box; default; disabled without selection | Saves/resumes job, then runs voice planning, input preparation, generation, recovery, checks, and pack publication. Label stays `Continue` while consequence is a potentially long generation. **V** | Rename dynamically to describe next outcome, e.g. `Prepare selected stories`, while preserving resume wording **H** |
| Dynamic `Cancel` | Bottom button box | Becomes Cancel import/voice matching/voice selection/preparation/generation/recovery/checks/save; cancellation waits for authoritative worker and resumable work is preserved. **V** | Keep dynamic action; parent ownership is sound |
| Native story-index file chooser | Launched from Browse | Filters JSONL plus All files; bad input returns inline error. **V** | Keep native |
| Native game-folder chooser | Launched from Choose game folder | Supplies explicit installation root; cancellation is no-op. **V** | Keep native, behind/adjacent to import |

Position finding: at the normal offscreen font, the dialog is fixed to a
700-pixel minimum while `minimumSizeHint()` is 816 pixels; the first row
compresses the combo to 76 pixels and importer buttons to about 136 pixels.
At 13/20/26-point fonts the minimum-width hints are 985/1367/1821 pixels, yet
the shown dialog remains 700 pixels wide and has no scroll area. **V** This is
not merely polish: labels and source choices can be clipped before 200% scale.

### DT-02: Embedded `VoiceAuditionPanel`

Source: `vntts/pregeneration_audition_ui.py:30-758`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| `Choose all automatically` | Top-right beside progress estimate | Commits candidate 0 for every remaining group and cancels active preview. **V** | Rename to state its rule (`Use recommended voices for all remaining`) and keep away from per-character primary choices; current label overstates inference **H** |
| `Play original game voice` | Above sample; hidden without checksum-verified anchor | Plays reference while leaving candidate choices enabled. **V** | Keep contextually |
| `Try another phrase` | Under shared sample; hidden if no alternate | Generates the same candidates on a second phrase only when requested. **V** | Keep contextually |
| `Play A` / `Play B` | Inside side-by-side candidate cards | Replays exact generated samples; failed candidates are removed from decision set. **V** | Keep |
| `Use A` / `Use B` | Inside each candidate card | Records displayed source, advances immediately, then batch-saves at the end. **V** | Keep; consider one selected-card + Continue pattern only if usability testing shows accidental choices **H** |
| `Neither sounds right` | Outcome row | Pages to the next pair, then offers narrator fallback; can relabel to `Use narrator without preview` after fallback-preview failure. **V** | Keep, but changing semantics on one button needs a specific accessible description/status announcement **H** |
| `Choose for me` | Outcome row | Selects the first currently displayed item, not necessarily the globally marked recommended candidate after paging. **V** | Rename to `Use first available` or change behavior to the stated recommendation; current label is not truthful enough |
| `Retry saving choices` | Outcome row; visible only after write failure | Reuses pending decisions and retries authoritative save. **V** | Keep contextually |
| Parent dynamic Cancel | Dialog footer, outside panel | Stops playback/previews and defers terminal cancel until active authoritative work settles; speculative prefetch does not block cancel. **V** | Keep |

The portrait and recommendation reasons are noninteractive evidence, but are
decision-critical. The portrait has an accessible name; the candidate reasons
and changed `Neither` semantics do not have explicit state descriptions. No
explicit shortcuts or tab-order test exists. Hidden panel controls remain in
the focus chain during source-only introspection, although hidden widgets are
normally skipped by Qt at actual key traversal. **G**

### DT-03/04: Narrator and character voice preview/assignment

Source: `vntts/voice_preview_ui.py:21-278`; launch integration at
`vntts/app.py:2255-2288` and `vntts/app.py:2390-2429`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| Editable `Narrator or character` combo | First form row | Selects existing character or accepts a typed name; changing it reloads current routing. **V** | Keep editable in general management; lock/replace with a fixed label in unknown-speaker recovery |
| `Candidate voice` combo | After routing explanation | Chooses a candidate and updates its description. **V** | Keep |
| `Preview text` editor | Mid-form, 100-pixel minimum | Starts with reusable sample but can be changed. Empty text is rejected at controller boundary. **V** | Keep |
| `Play selected voice` | Full-width form row | Freezes exact target/voice/text identity, disables mutable controls, and starts async preview. **V** | Keep as preview primary action |
| `Stop preview` | Separate full-width row; disabled when idle | Cancels the matching future/backend and close waits for completion. **V** | Keep contextually; hide when idle only if focus stability is preserved **H** |
| `Use selected Narrator fallback voice` / `Use for this character` | Full-width row | Persists selected voice synchronously; failures stay inline. **V** | Keep as assignment primary action; visually separate preview from persistence |
| `Always use live TTS for Narrator (bypass pregenerated tracks)` | Under assignment; narrator-only | State is persisted only when the assignment button is pressed; hidden for characters. **V** | Rename in user language and place inside a Narrator routing section. Current long technical label drives severe width overflow |
| `Use default Narrator voice` / `Use automatic voice routing` | Under checkbox | Clears explicit assignment; narrator clear also clears force-live. **V** | Keep as secondary reset, not equal visual weight |
| `Close` | Standard footer | During preview it requests stop and defers close. Outside preview it closes even if the force-live checkbox was changed but not assigned. **V** | Keep; dirty routing state needs either immediate apply or visible Save semantics **H** |

The same adaptive dialog serves two different missions. In unknown-speaker
recovery, `open_speaker_mapping()` records `initial_character`, but the dialog
allows the user to switch/type another target; after close the caller checks
only whether the original target was assigned (`vntts/app.py:2409-2428`). A
valid assignment to a different character therefore returns `False` and opens
the original prompt again. **V** The smallest coherent fix is contextual target
locking, not a new dialog.

At 13/20/26-point fonts, voice-dialog minimum-width hints are 681/962/1310
pixels while the actual width remains 560. There is no scroll area. **V** The
long force-live checkbox is the dominant constraint.

### DT-04/05 transient voice prompts

Source: `vntts/app.py:1617-1727` and `vntts/app.py:2289-2372`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| Preflight `Assign voices...` | Floating always-on-top warning | Opens general mapping at the first unresolved speaker, then rechecks the scope before live start. **V** | Keep; use the contextual locked-target mode |
| Preflight `Use narrator for all` | Same warning, accept role | Approves only the current verified speaker scope; stale approval is rejected/rechecked. **V** | Keep, but emphasize session scope in button label **H** |
| Preflight `Cancel live reading` | Same warning, escape action | Leaves live reading stopped and reports why. **V** | Keep |
| Unknown speaker `Choose voice...` | Floating always-on-top warning | Stops/resumes live mode around mapping. **V** | Keep; open locked target |
| Unknown speaker `Continue with narrator` | Same warning and Escape default | Explicitly allows fallback and resumes. **V** | Keep; button accurately describes immediate consequence |
| Window close on unknown-speaker prompt | Native close chrome | Is treated like narrator continuation unless mapping is already in progress. **V** | Question: closing a warning silently takes the accept/fallback path; make close equivalent to an explicitly labelled safe choice only after usability validation **H** |

### DT-06: Uncertain OCR review

Source: `vntts/ocr_review_ui.py:23-285`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| Pending sample list | Left column | Selects current evidence; row includes speaker, confidence, and text excerpt. **V** | Keep; add explicit accessible name and label/description |
| Detected text read-only editor | Right form | Allows inspection/copy. **V** | Keep, but it should not precede correction fields in keyboard order unless copying source is a common task **H** |
| Correct speaker field | Right form | Defaults to detected speaker; changed full value becomes a reusable exact replacement. **V** | Keep; add accessible name/buddy |
| Correct text editor | Right form | Defaults to detected text; changed full value becomes a reusable exact replacement. **V** | Keep; explain exact phrase matching adjacent to this decision |
| `Save correction for` scope combo | Below correction fields | Defaults to current profile if provided, otherwise `All games`. **V** | Keep; never silently fall back to global when an active profile was expected |
| `Save correction and resolve` | First outcome button | If either correction changed, performs correction upsert plus resolution asynchronously. If nothing changed, opens `No correction entered`. **V** | Keep, but disable until a meaningful edit or rename action state; modal no-op warning is avoidable |
| `Resolve without correction` / `Confirm resolve without correction` | Second outcome button | Two-step inline confirmation, reset on sample change, async resolve. **V** | Keep |
| `Close` | Standard footer | Leaves pending sample untouched; defers during active write. **V** | Keep |
| Native `No correction entered` information dialog | Launched by unchanged Save | Tells user to edit or resolve. **V** | Replace with disabled-action explanation or inline status; no new modal needed |

The preview image is scaled only in `show_sample()`; the dialog has no
`resizeEvent`, so later resizing does not rescale the source image. **V** The
sample list, screenshot, correction fields, and scope have no explicit
accessible names/buddies. At 26-point font the minimum hint becomes 1271x784,
larger than common 1280x720 work areas, and there is no scroll area. **V**

### DT-07: OCR correction editor

Source: `vntts/ocr_corrections_ui.py:20-324`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| `Global` tab | Main body; selected by default | Edits rules for every profile. **V** | Keep, but default to current-profile tab when launched with a profile; global is the higher-blast-radius choice **H** |
| Current-profile tab | Beside Global; disabled without profile ID | Edits overrides for active profile. **V** | Keep; disabled reason should be exposed, or omit tab when no profile exists |
| Global two-column table (`OCR text`, `Replace with`) | Global tab | Directly edits all global rules. **V** | Keep; add accessible name/description |
| Profile two-column table | Profile tab | Directly edits profile overrides. **V** | Keep; add accessible name/description |
| Global `Add` | Below global table | Appends and begins editing a blank row; Insert shortcut duplicates it. **V** | Keep |
| Global `Remove selected` | Beside Add | Removes selected rows; Control+Delete shortcut; changes remain undoable via Cancel before Save. **V** | Keep |
| Profile `Add` | Below profile table | Same action in profile scope. **V** | Keep |
| Profile `Remove selected` | Beside Add | Same action in profile scope. **V** | Keep |
| `Save` | Standard footer | Validates both scopes, focuses first error, disables UI during async atomic replace, restores for retry. **V** | Keep |
| `Cancel` / `Discard changes` | Standard footer | First dirty close/cancel changes label and explains loss; second confirms discard. **V** | Keep |

Validation does not rely only on color: it colors exact cells, adds tooltips,
lists every error in status text, and focuses the first invalid cell. **V** No
search/import/export control is justified by current evidence; do not add one
until actual rule-set size makes table scanning fail. **H**

### DT-08: Dialogue history

Source: `vntts/history_ui.py:21-250`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| Search field | Above two-column content | Live case-insensitive speaker/dialog search; explicit label buddy and accessible name. **V** | Keep |
| History entry list | Left column | Selects session entry; 750ms refresh preserves selected ID and scroll. **V** | Keep; add label/buddy or accessible description |
| Selected details read-only editor | Right column | Shows speaker, raw ISO timestamp, and full text; explicit label buddy/name. **V** | Keep; format timestamp for users while retaining exact value for export **H** |
| `Replay selected` | Outcome row | Runs replay off UI thread and retries after failure. **V** | Keep; disable correctly in empty state |
| `Stop / skip replay` | Beside Replay; disabled idle | Calls request-scoped stop and abandons matching runner; close waits for stop result. **V** | Rename `Stop replay`; `skip` suggests navigation rather than cancellation **H** |
| `Export...` | Same outcome row | Opens native Text/JSON save chooser and appends selected extension. **V** | Keep; move away from replay transport if visual hierarchy is unclear **H** |
| `Close` | Standard footer | Stops an active replay before closing and restores live capture in caller. **V** | Keep |
| Native export chooser | Save dialog | Text and JSON filters; default `dialogue-history.txt`; cancel no-op. **V** | Keep native |
| Native `Unable to export history` warning | Export error | Shows raw `OSError`; retry is reopening Export. **V** | Keep error local, but include destination/action guidance when error is not self-explanatory **H** |

Empty-state bug: `visible_entries` starts as `[]`; `refresh()` returns immediately
when `history.search()` also returns `[]`, so `show_entry(-1)` never disables the
button. Offscreen runtime confirmed `Replay selected` is enabled with no entry,
although activating it is a silent no-op. **V** (`vntts/history_ui.py:34-100`)

### DT-09: Live diagnostics

Source: `vntts/diagnostics_ui.py:15-228`; app integration at
`vntts/app.py:1779-1851`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| Recognized-text read-only editor | Middle of 14-row form | Displays/copies OCR result. **V** | Keep; give accessible name/buddy |
| `Refresh now` | Bottom status row | Emits fresh capture request, disables itself, starts 10-second tokenized timeout. **V** | Keep; integrated timeout must restore the concealed window |
| `Close` | Standard footer | Stops timeout generation and closes. **V** | Keep |

Noninteractive but decision-critical evidence includes capture preview, speaker,
confidence, preprocessing profile, selected voice, capture/OCR/synthesis/playback
latencies, capture interval, first audio, queue depth, focus, corrections,
warning, and refresh status. Keep the user-facing fields; move preprocessing and
low-level timing into a collapsed technical disclosure if ordinary-player
observation confirms overload. **H**

Three verified stale/recovery defects:

1. Refresh hides the window before capture. The dialog's timeout calls only
   `_finish_refresh()` and never `restore_after_capture()`. If the app-side
   capture never emits success/failure, the timeout enables Retry in a window
   that remains hidden (`vntts/diagnostics_ui.py:122-134`,
   `vntts/app.py:1791-1810`). **V**
2. `set_snapshot()` replaces the preview only when `snapshot.image is not None`.
   A later image-less snapshot therefore keeps the previous screenshot beside
   new text/metrics (`vntts/diagnostics_ui.py:157-186`). **V**
3. `set_snapshot()` does not clear a prior transient warning. A successful
   refresh can leave a previous capture error visible (`vntts/diagnostics_ui.py:157-196`). **V**

The fixed 14-row form is not scrollable. It already expands from requested
700x540 to 700x593 at the default offscreen font. At 13/20/26 points its minimum
height is 668/848/998 pixels. **V**

### DT-10: Diagnostics and logs / support

Source: `vntts/support_ui.py:15-222`; export integration at
`vntts/app.py:2430-2543`.

| Control | Current position/state | Mission and verified behavior | Decision |
| --- | --- | --- | --- |
| `Runtime log` tab | Main body, default | Shows periodically refreshed redacted event text. **V** | Keep |
| Runtime event read-only editor | Runtime tab | Supports selection/copy; appended events preserve manual viewport/selection. **V** | Keep; add accessible name |
| `Show N new event(s)` | Under log, hidden until needed | Jumps to end without stealing current selection beforehand. **V** | Keep contextually |
| `Problem report` tab | Main body | Contains instructions only; it is not a report form. **V** | Rename `How to report a problem` or merge the short help above actions **H** |
| Problem-report read-only text editor | Help tab | Displays one paragraph but takes a tab stop like editable content. **V** | Replace with selectable wrapping label unless keyboard text selection is a real requirement **H** |
| `Live diagnostics` | Shared action row below tabs; Ctrl+D | Opens DT-09 in a separate window; only this launcher disables until result. **V** | Keep, but avoid duplicate top-level tray entry after ownership reconciliation |
| `Export support report` | Same row; Ctrl+E | Native ZIP chooser, background privacy-safe build, local cancel/failure/success/retry state. Builder appends `.zip`. **V** | Keep as primary support action |
| `Open settings folder` | Same row; Ctrl+Shift+O | Opens platform folder and returns local result. **V** | Keep as advanced recovery action here; question duplicate tray action |
| `Close` | Standard footer | Hides/closes support surface; timer stops on hide. **V** | Keep |
| Native support ZIP chooser | Export action | Default `vntts-support.zip`; cancellation returns action to ready. **V** | Keep native |

At 26-point font the minimum-width hint is 1022 pixels while the actual window
remains 760 and has no scroll area. The three action buttons share one fixed
horizontal row and are the main width constraint. **V**

## Entry-point and duplication accounting

Every related top-level entry was found in `vntts/app.py:1145-1263` and
`vntts/dashboard_ui.py:150-238`:

| Entry control | Current destination | Review |
| --- | --- | --- |
| Dashboard `Prepare offline audio...` | DT-01 | Keep: ordinary-player, outcome-oriented placement |
| Tray `Prepare offline audio...` | DT-01 | Merge/remove after runtime-navigation review; exact duplicate **H** |
| Dashboard `Narrator voice` | DT-03 | Keep only if dashboard advanced disclosure remains the owner |
| Tray `Choose narrator voice...` | DT-03 narrator initial target | Duplicates dashboard but wording differs |
| Tray `Manage character voices...` / dynamic `Manage voice for <speaker>...` | DT-03 | Keep dynamic recovery route; merge static general route with narrator management **H** |
| Tray `OCR corrections...` | DT-07 | Keep under a single OCR/support owner; currently tray-only |
| Tray `Review uncertain OCR...` | DT-06 | Keep when pending work exists; ideally show pending count/state **H** |
| Tray `Dialogue history...` | DT-08 | Keep until dashboard ownership is decided; current-session task is frequent enough to justify reachability **H** |
| Dashboard `Support and logs` | DT-10 | Keep as ordinary-player support owner |
| Tray `Diagnostics and logs...` | DT-10 | Exact destination with different wording; merge terminology |
| Tray `Live diagnostics...` | DT-09 | Duplicates a button inside DT-10; retain only if direct expert access is measurably useful **H** |
| Tray `Open settings folder` | Native folder open | Duplicates DT-10 action; move under support unless used routinely **H** |

No dashboard entries exist for OCR review/corrections or history. That may be
correct progressive disclosure, but the tray is currently functioning as an
unstructured advanced menu. Final ownership must be decided with the
runtime/setup audit, not locally.

## Ranked findings

### P1 - workflow/recovery failures

1. **Diagnostics timeout can strand its concealed window.** Verified integration
   defect; Retry becomes enabled while invisible. Restore on every terminal
   refresh result, including dialog-owned timeout.
2. **Diagnostics can mix stale screenshot/warning with a fresh snapshot.** Clear
   or explicitly mark absent image and transient warning on each authoritative
   result.
3. **Unknown-speaker mapping permits editing a different target.** The caller
   checks only the original target and then prompts again. Lock target in this
   context; retain editable target in general management.
4. **Daily dialogs are not responsive at large text, and offline selection is
   width-constrained even near default size.** Add scrolling/wrapping/reflow;
   do not solve by growing fixed minimums beyond common displays.

### P2 - misleading hierarchy or avoidable friction

5. **Offline source acquisition exposes four peer controls in one row.** Make
   automatic import the main recovery and group manual file/folder alternatives.
6. **`Choose for me` does not name its actual rule.** It uses the first currently
   displayed candidate; after `Neither`, that is not the card marked globally
   recommended. Align label and behavior.
7. **Voice force-live state has hidden Save semantics.** Checkbox edits persist
   only through the assignment button; Close silently drops them. Either apply
   immediately or present an explicit routing Save boundary.
8. **OCR correction editor defaults to Global despite an active profile.** Put
   lower-blast-radius current-profile corrections first when one exists.
9. **OCR Review offers an enabled Save that often opens a no-op information
   modal.** Disable/explain until corrected content differs.
10. **Dialogue History enables Replay in the empty initial state.** Initialize
    action state before the equality early-return.
11. **Diagnostics diagnoses but provides no direct remediation.** Permission and
    unavailable-capture warnings are text only despite existing permissions,
    calibration, settings, and readiness actions elsewhere. Route typed warning
    identities to one contextual fix; do not add a permanent row of buttons.
12. **Support/tray duplicate diagnostics and settings-folder ownership.** Keep
    Support as the ordinary-player home and preserve direct tray actions only
    with usage evidence.

### P3 - accessibility and terminology consistency

13. Add explicit accessible names/descriptions and label buddies for the OCR
    sample list/correction fields/scope, both correction tables, diagnostic OCR
    text/preview/status semantics, support logs/help, and voice form inputs.
14. Rename `Stop / skip replay` to `Stop replay`; rename the help-only `Problem
    report` tab; standardize `Support and logs` versus `Diagnostics and logs`.
15. Rescale OCR evidence on window resize and verify focus/announcement when
    dynamic buttons relabel or appear.

## Missing-capability evidence

Capabilities justified by an observed dead end:

- **Terminal refresh restoration** in diagnostics: required because the
  existing timeout produces an invisible retry state. This is behavior, not a
  new button.
- **Responsive scrolling/reflow** for DT-01, DT-03, DT-06, DT-09, and DT-10:
  required by measured minimum hints exceeding their windows/work areas.
- **Context-locked unknown speaker target**: required because a currently valid
  interaction can save another target and fail the initiating mission.
- **One contextual diagnostics remediation action**: required because the
  mandated diagnose/fix/verify mission currently ends in instructional text.
- **Explicit freshness/absence semantics for diagnostic evidence**: required to
  prevent old capture/error evidence from being presented as current.

Not justified yet: OCR rule import/export/search, persistent multi-session
history, manual voice model parameters, a custom file browser, a new support
wizard, or per-stage offline configuration. Existing controls and automation
cover those speculative needs until real failure evidence appears.

## Test and visual gaps

- No test exercises `conceal_for_capture()` followed by the dialog's own timeout;
  current timeout test calls `request_refresh()` directly while visible.
- No test sends a fresh image-less diagnostic after an image or verifies warning
  clearing after successful refresh.
- No test changes the target inside unknown-speaker mapping and checks the
  initiating speaker outcome.
- No daily-tools test verifies 150%/200% fonts, clipping, reflow, scroll reach,
  or minimum work-area fit. Offscreen measurements above show real failures.
- OCR Review has no resize-after-selection test; screenshot scaling is only
  exercised at initial selection.
- Accessibility coverage is strong only for History search/details and Support
  action buttons. No screen-reader tree or keyboard-only mission is tested for
  offline preparation, voice audition, voice assignment, OCR review,
  corrections, diagnostics, or both support tabs.
- No Windows/macOS native-dialog screenshots were captured. File-filter order,
  extension display, overwrite confirmation, focus return, and fullscreen
  floating-prompt placement remain unverified.
- Voice audition has behavioral tests for all major branches but no visual
  evidence for long names, portraits, long recommendation text, alternate
  phrase, narrator fallback, or save failure in a compact viewport.
- Offline preparation reports stage text but has no runtime screenshot with a
  large real selection or a long-running generation, and no test of whether
  progress is understandable without logs.
- Support tests verify a 620x420 window only at normal font; this does not prove
  large-text accessibility.

## Cross-surface concerns for primary integration

1. **Ownership:** dashboard Support, tray Support, tray Live diagnostics, and
   Support's Live diagnostics button overlap. Dashboard Narrator voice, tray
   narrator voice, tray character voices, runtime prompts, and offline audition
   also overlap. Choose one ordinary-player owner per mission, retain contextual
   recovery entry points, and treat tray duplicates as advanced shortcuts only
   with evidence.
2. **Terminology:** `Prepare offline audio`, `pregenerated tracks`, `generated-first
   routing`, `live TTS`, `live fallback`, `automatic routing`, and `narrator
   fallback` describe overlapping concepts at different technical levels. The
   final pass needs one player vocabulary.
3. **Busy/close contract:** offline preparation, voice preview, OCR writes,
   replay, diagnostics refresh, and support export all implement local async
   lifecycles differently. Preserve request-scoped cancellation and authoritative
   completion, but standardize visible busy, retry, and deferred-close language.
4. **Status ownership:** some results remain inside the initiating dialog;
   others also overwrite app status or emit tray notifications. Decide which
   events require global notification so successful support export, offline pack
   activation, OCR close, and voice assignment do not compete.
5. **Accessibility:** dynamic relabeling is widespread. Screen readers need
   explicit announcements for Cancel stage, Neither/narrator fallback, Resolve
   confirmation, Stop availability, warning appearance, and hidden/new-event
   actions.
6. **Scaling strategy:** dashboard already owns a scrollable responsive pattern.
   Reuse it rather than inventing per-dialog size hacks.

## Completion gate for this private scope

Every interactive control and native-dialog call site in the assigned surfaces
is accounted for above. Source and focused test behavior were checked, but the
scope is not ready to call visually complete until the P1 integration defects
are covered and cross-platform 100%/150%/200% screenshot plus keyboard/screen-
reader passes exist.
