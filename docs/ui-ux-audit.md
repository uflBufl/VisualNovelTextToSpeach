# Desktop interface UX audit

This document records the baseline desktop-interface audit performed on
2026-08-22. It covers every shipped Qt surface and the operator-facing CLI
entry points that open those surfaces. It is a design and verification input,
not a claim that every workflow has already been redesigned.

## Audit method

The review combined:

- source inspection of widget hierarchy, signal boundaries, persistence,
  cancellation, close behavior and authoritative writes;
- existing UI and domain tests, including accessibility, stale-result and
  compact-window coverage where present;
- offscreen rendering of the three current high-consequence evidence-review
  surfaces against real or checksum-bound fixtures;
- read-only inspection of real completed and incomplete authoring sessions.

The rendered baselines were:

- `vntts-listen`: 900 x 520, one incomplete blinded trial;
- `vntts-reference-audit`: 1040 x 650, the four-group Character Story audit;
- `vntts-reference-review`: 780 x 540, the incomplete Dobharchu quality review.

The audit evaluates five states for each surface: useful data, empty data,
work in progress, failure and constrained window size. A source or test result
is recorded as a gap when the state is not yet exercised directly.

## Shared interaction contract

High-consequence review interfaces must use the same operator contract:

1. Show overall progress, current-item position and the exact decision scope.
2. Put the current evidence in one stable card. Keep identifiers, hashes,
   diagnostics and provenance in a collapsed technical disclosure.
3. Keep playback and navigation spatially stable while playback and saving run.
4. Distinguish sample-level observations from terminal cohort or reference
   decisions. Never make the scope implicit in a generic Accept button.
5. Explain a disabled action next to the action. Do not require color, a tooltip
   or a technical log to discover why it is unavailable.
6. Prepare audio, save authority and refresh projections outside the Qt thread.
   Show the active phase, defer close while an authoritative commit is in
   progress, and provide an in-dialog retry after a transient failure.
7. Bind every decision and replay to the exact checked authority bytes. UI work
   must not weaken queue, state, WAV, hidden-key or lease validation.
8. Expose discoverable shortcuts, accessible names/descriptions and a deliberate
   focus order. Table editing must never steal a decision shortcut.

Daily application and setup surfaces use the same status, busy, recovery,
accessibility and compact-layout conventions, but do not need the review ledger.

## Surface matrix

| Surface and entry point | Primary operator task | Current strengths | Findings and required change | Priority |
| --- | --- | --- | --- | --- |
| Specialist cohort review, `vntts-review-bundle` | Hear a checksum-bound sample set and make one cohort decision | Dedicated current-sample card, overall/current progress, heard ledger, stable navigation, async replay/save, terminal confirmation, explicit bad-sample and more-sample outcomes, accessibility and shortcuts | Use as the reference implementation. Keep real-data compact, failure and long-save regressions; avoid reintroducing the old Rhiannon-only or workbench-embedded workflow. | Reference |
| Authoring workbench, `vntts-authoring-workbench` | Inspect generation state, run selected generation and repair individual outcomes | Background authority projection, immutable replay buffer, CAS-bound review, visible reasons, filters, accessibility and safe close behavior | The window still combines generation, references, individual review, cohort creation, cohort review, history and technical logs. Route bundle review to the dedicated reviewer and reduce this window to generation/status plus exceptional item repair. Preserve stable review controls and reasons while simplifying the layout. | P1 |
| Blind listening, `vntts-listen` | Compare hidden A/B audio and record one blinded preference | Clear A/B labels, seek/play controls, explicit `Neither acceptable`, deterministic hidden-key/report validation, async authoritative save, truthful report-recovery phase and deferred close | Progress still lacks remaining work and current-trial hierarchy. Bring its evidence card, progress and keyboard/accessibility treatment up to the shared review contract without changing blinding. | P2 |
| Failure reference audit, `vntts-reference-audit` | Compare opaque reference candidates for a failed speaker group | Stable current-candidate card, candidate/group progress, complete heard ledger, decisions locked until every candidate ends, collapsed affected lines, checksum-bound in-memory playback, blinded labels, async save and safe close | Retain as a second shared-contract reference. Any future persisted heard state must remain non-authoritative and must not reveal the private candidate mapping. | Reference |
| Source reference quality, `vntts-reference-review` | Judge original and generated evidence for one source reference | Exact portrait support, separate original/generated playback, explicit accept/reject/need-another decisions, async authoritative save, in-dialog retry and deferred close | Missing portraits leave a large blank region; excluded diagnostics dominate the card; generated variants are compressed into combined strings. The copy says to listen before deciding while tests deliberately allow an immediate decision. Compact absent evidence and settle one explicit unheard-evidence policy before changing gating. | P2 |
| Main dashboard and compact controller | Start/stop reading and understand current speech state | Textual state, current dialogue, source/voice/latency fields, scrollable short-window layout and explicit emergency stop | Six peer runtime actions and six peer setup actions provide weak hierarchy. During initialization they are disabled without an adjacent reason. Make live/read the primary mode action, group transport and emergency actions separately, and expose one persistent readiness/recovery reason in both full and compact modes. | P1 |
| Settings | Configure capture, OCR, audio routing, manifests and backend | Grouped sections, scrolling layout, dependent controls and standard Save/Cancel | The long form has no section navigation or search. Several paths lack browse and immediate validation. Save reports one modal error at a time and restart scope is stated only at the bottom. Add section navigation, consistent path pickers, inline validation summary and per-setting restart markers. | P1 |
| Onboarding wizard | Produce a verified first OCR-to-speech setup | Cancellable stale-safe diagnostics, explicit calibration, guarded end-to-end test and safe test cancellation | The configuration page is dense and the wizard shows no `step N of M` orientation. Modal validation reveals errors one at a time. Add step progress, a compact-window scroll gate and inline error summary. Keep the existing task-runner authority and cancellation model. | P2 |
| Readiness | Verify the complete runtime chain and open remediation | Background stale-safe checks, cancel/retry, textual OK/warning/error status and a scrollable result table | All remediation buttons are always present and are not bound to the selected failing row. Associate each error with its relevant action and make unavailable actions explain themselves. | P2 |
| Model and voice assets | Download/verify a model and import a voice pack | Download and checksum verification run in the background with progress and cancellation | Voice-pack and character-reference import remain synchronous and may hash/copy large files on the Qt thread. The editable manifest path lacks a browse/validate affordance. Move imports to the shared task runner, retain progress and safe close, and validate the selected manifest inline. | P1 |
| Voice preview and assignment | Preview a voice and assign or restore routing | Preview synthesis is asynchronous and routing differences for Narrator are explained | Target, candidate and Close remain mutable during an in-flight preview; assignment handlers are synchronous; there is no Stop. Freeze or snapshot the preview controls, add stop/cancel, and display the exact previewed target separately from the current editable selection. | P2 |
| OCR uncertain-sample review | Correct or resolve captured low-confidence OCR evidence | Side-by-side screenshot, detected result, corrected result and global/profile scope | Image decode and authoritative correction/resolution writes are synchronous. There is no pending-count progress, shortcut path or confirmation for resolving without a correction. Add current/remaining progress, background writes and an explicit resolve-without-rule confirmation. | P2 |
| OCR correction editor | Maintain global and profile replacement rules | Clear scope tabs, editable two-column rules and Save/Cancel semantics | No search, import/export or unsaved-close indication; row validation is deferred to Save and shown one error at a time. Add inline row errors and keyboard-accessible add/remove. Import/export is optional until rule sets become large. | P3 |
| Dialogue history | Search, replay and export the current session | Live search, preserved selection, readable detail and text/JSON export | Replay is a synchronous callback with no preparing/playing/stop state; a slow backend can freeze the dialog. Timer refresh rebuilds the list when data changes and can interrupt reading. Use typed async playback state and avoid automatic scroll/selection movement while the operator is inspecting an older entry. | P2 |
| Live diagnostics | Inspect the latest frame, OCR, routing and latency | Full capture preview, textual metrics, permission warnings and deliberate self-concealment during capture | Refresh has no timeout/cancel or launch identity inside the dialog. If the external handler never reports, Refresh remains disabled indefinitely. Use the shared latest-task lifecycle or a bounded external request token and show a Retry state. | P2 |
| Support and logs | Inspect redacted runtime events and export support evidence | Privacy-safe explanation, separate help/log tabs and direct support actions | Every new log event replaces the whole text document and scrolls to the end, disrupting selection and inspection. Preserve manual scroll/selection and show `new events` instead of forcing the viewport. Distinguish export progress and failure in this dialog. | P2 |
| Game profiles | Create, duplicate, rename, remove and activate a profile | Small focused dialog, destructive confirmation and useful stored-settings summary | Four management actions have equal visual weight and the active profile is not explicitly distinguished from the selected stored profile. Separate activation from management and label active/current state. | P3 |
| Calibration overlay and review | Select the full dialogue region and verify OCR | Frozen screenshot, normalized selection, clear draw/retry language and OCR preview | OCR is executed synchronously while constructing the review dialog, leaving no visible progress after selection. Move recognition to a worker, show the crop immediately, and disable Save until a current OCR result or an explicit capture-only override is available. | P1 |
| macOS permissions | Understand and request screen/accessibility permissions | Text status, direct Request/Open Settings actions and restart guidance | Request calls are synchronous platform operations and the two permissions use repeated peer controls. Low risk, but add per-row busy/error state and refresh automatically after returning from Settings when feasible. | P3 |
| Diagnostics/log support actions | Export evidence or open related tools | Clear privacy boundary and distinct tools | Result, progress and failure are handled outside the dialog, so the initiating window gives no acknowledgement. Add an operation banner or disable only the active action until its result returns. | P3 |

## Accessibility and keyboard baseline

The authoring workbench, specialist cohort reviewer, onboarding and Settings
contain explicit accessibility or focus-order coverage. Most smaller dialogs do
not currently set accessible names/descriptions or explicit tab order. Native
labels and button text help, but composite selectors, tables, preview regions,
status banners and icon/color state need explicit metadata.

Apply the existing invariants in `desktop-accessibility.md` to every composite
form row. Add one keyboard-only offscreen test per operator task, not one test
per widget. A passing test must reach the primary action, hear or inspect the
current evidence, recover from a blocked action and close without using a
mouse.

## Implementation order and completion gates

1. Fix the remaining blocking authoritative interactions first: asset import,
   calibration OCR, OCR writes and history replay. A heartbeat test must show that the
   Qt event loop remains responsive through delayed I/O; close/retry behavior
   must be explicit.
2. Normalize the three evidence-review surfaces around the specialist cohort
   contract. Preserve blinding and exact authority. Real incomplete and complete
   sessions must retain their hashes and decisions.
3. Simplify the workbench boundary by opening dedicated reviewers rather than
   duplicating them. Keep generation, status and exceptional item repair in the
   workbench.
4. Normalize dashboard, Settings and onboarding. Verify 100%, 150% and 200% DPI,
   keyboard-only use, screen-reader names, compact height and error recovery.
5. Apply the lighter consistency pass to OCR, history, diagnostics, support,
   profiles, voice preview, permissions and corrections.

An interface backlog item is complete only when populated, empty, busy, failure
and compact-window states are covered; an offscreen responsiveness test guards
the UI thread; keyboard and accessibility metadata are verified; and the
operator-facing behavior is recorded in the relevant durable documentation.
