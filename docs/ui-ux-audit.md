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
| Authoring workbench, `vntts-authoring-workbench` | Inspect generation state, run selected generation and repair individual outcomes | Background authority projection, immutable replay buffer, CAS-bound individual review, visible reasons, filters, accessibility and safe close behavior; its only cohort action builds an exact bundle in the background and opens the modal specialist reviewer | Keep cohort playback and decisions exclusively in `vntts-review-bundle`. Further workbench simplification may reorganize references/history/technical details, but must not reintroduce cohort authority or weaken individual-item authority. | P2 |
| Blind listening, `vntts-listen` | Compare hidden A/B audio and record one blinded preference | Clear A/B labels, seek/play controls, explicit `Neither acceptable`, deterministic hidden-key/report validation, async authoritative save, truthful report-recovery phase and deferred close | Progress still lacks remaining work and current-trial hierarchy. Bring its evidence card, progress and keyboard/accessibility treatment up to the shared review contract without changing blinding. | P2 |
| Failure reference audit, `vntts-reference-audit` | Compare opaque reference candidates for a failed speaker group | Stable current-candidate card, candidate/group progress, complete heard ledger, decisions locked until every candidate ends, collapsed affected lines, checksum-bound in-memory playback, blinded labels, async save and safe close | Retain as a second shared-contract reference. Any future persisted heard state must remain non-authoritative and must not reveal the private candidate mapping. | Reference |
| Source reference quality, `vntts-reference-review` | Judge original and generated evidence for one source reference | Exact portrait support, separate original/generated playback, explicit accept/reject/need-another decisions, async authoritative save, in-dialog retry and deferred close | Missing portraits leave a large blank region; excluded diagnostics dominate the card; generated variants are compressed into combined strings. The copy says to listen before deciding while tests deliberately allow an immediate decision. Compact absent evidence and settle one explicit unheard-evidence policy before changing gating. | P2 |
| Main dashboard and compact controller | Start/stop reading and understand current speech state | Textual state, current dialogue, source/voice/latency fields and scrollable short-window layout; Start/Stop live is the single primary action, Read once is its explicit alternative, playback and emergency controls are grouped separately, and setup/support remains available during failure | Full and compact modes expose a persistent adjacent availability/recovery reason and accessible descriptions. Keyboard activation, short height and 100%, 150% and 200% scaled-font layouts are covered. Preserve this hierarchy as runtime actions evolve. | Complete |
| Settings | Configure capture, OCR, audio routing, manifests and backend | Keyboard-accessible section navigation over one canonical form, scrolling grouped layout, dependent controls, standard Save/Cancel and a shared labelled Browse contract for all eight filesystem inputs | Validation reports every current error inline, Save focuses the first invalid control without modal-warning churn, and runtime-bound fields carry visible and accessible restart markers. Tests cover path selection, multiple simultaneous errors, keyboard correction and 100%, 150% and 200% scaled-font layouts. | Complete |
| Onboarding wizard | Produce a verified first OCR-to-speech setup | Cancellable stale-safe diagnostics, explicit calibration, guarded end-to-end test and safe test cancellation; every page now shows `Step N of 5`, and the single dense configuration page scrolls independently in compact windows | Configuration validation reports all current errors inline and focuses the first invalid control without modal-warning churn. Tests preserve the diagnostics/test lifecycle and cover keyboard progress plus 100%, 150% and 200% scaled-font layouts. | Complete |
| Readiness | Verify the complete runtime chain and open remediation | Background stale-safe checks, cancel/retry, textual OK/warning/error status and a scrollable result table; diagnostics carry an explicit remediation identity, the first actionable error is selected automatically, and one contextual action explains both its enabled and disabled states | Keep new diagnostics on the typed remediation boundary instead of matching names or message text. Tests cover populated, ready, busy, cancelled, failed, compact, keyboard and accessibility states. | Complete |
| Model and voice assets | Download/verify a model and import a voice pack | Download, checksum verification, voice-pack import and character-reference import run in the background with visible progress, safe close and retry | The editable manifest path still lacks a browse/validate affordance. Add the same composite path selector and inline validation used by Settings. | P2 |
| Voice preview and assignment | Preview a voice and assign or restore routing | Preview synthesis is asynchronous, exact target/candidate/text identity remains visible, mutable routing controls freeze during playback, request-scoped Stop reaches the loaded backend, close is deferred and Narrator routing differences are explained | Assignment remains deliberately independent from preview evidence: after playback ends the operator may change the candidate before saving. If assignment persistence becomes measurably slow, move it to the shared authoritative task lifecycle without making preview completion authorize it. | P3 |
| OCR uncertain-sample review | Correct or resolve captured low-confidence OCR evidence | Side-by-side screenshot, detected/corrected result, global/profile scope and background authoritative correction/resolution writes with retry and safe close; the queue now shows pending and `Current N of M` progress | `Resolve without correction` uses a non-blocking two-step confirmation that explains no reusable rule will be saved; changing samples resets that confirmation. Preserve this distinction from `Save correction and resolve`. | Complete |
| OCR correction editor | Maintain global and profile replacement rules | Clear scope tabs, editable two-column rules, background Save with retry/deferred close and standard Save/Cancel semantics | Global and profile row errors are reported together and marked on the exact cells; Insert adds a row and Control+Delete removes selected rows. Dirty Cancel/close requires an explicit second `Discard changes`. Search/import/export remains optional until real rule sets justify it. | Complete |
| Dialogue history | Search, replay and export the current session | Live search, readable detail, text/JSON export and async replay with progress, retry and deferred close | Timer refresh preserves the exact older selection, details and scroll position while new entries arrive, without an intermediate deselection. Replay Stop is intentionally absent: the current controller exposes only shared emergency stop, not request-scoped cancellation for the exact history replay. | Complete |
| Live diagnostics | Inspect the latest frame, OCR, routing and latency | Full capture preview, textual metrics, permission warnings, deliberate self-concealment during capture and a bounded refresh token that always returns to Retry after timeout/failure | A future enhancement may offer cancellation when the capture boundary itself has a safe request-scoped cancellation token; do not cancel shared live capture by inference. | P3 |
| Support and logs | Inspect redacted runtime events and export support evidence | Privacy-safe explanation, separate help/log tabs, direct support actions, preserved manual selection/scroll, explicit new-event navigation and in-dialog export progress/cancellation/failure/retry | Keep future support actions on the same local status boundary instead of reporting only through the tray/dashboard. | P3 |
| Game profiles | Create, duplicate, rename, remove and activate a profile | Small focused dialog with explicit active versus selected state, one primary `Use selected profile` action, a separate management group, destructive confirmation and prevention of active-profile removal | A later visual pass may add profile icons or richer metadata, but activation and management semantics are now explicit without relying on color. | P3 |
| Calibration overlay and review | Select the full dialogue region and verify OCR | Frozen screenshot appears immediately, normalized selection, background OCR progress, clear draw/retry language and explicit `Save region without OCR preview` fallback | Add keyboard/accessibility coverage for drawing/retry and verify the overlay at multiple-display/DPI boundaries. | P2 |
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

1. Normalize the three evidence-review surfaces around the specialist cohort
   contract. Preserve blinding and exact authority. Real incomplete and complete
   sessions must retain their hashes and decisions.
2. Simplify the workbench boundary by opening dedicated reviewers rather than
   duplicating them. Keep generation, status and exceptional item repair in the
   workbench.
3. Normalize dashboard, Settings and onboarding. Verify 100%, 150% and 200% DPI,
   keyboard-only use, screen-reader names, compact height and error recovery.
4. Apply the lighter consistency pass to OCR, history, profiles, voice preview,
   permissions and corrections.

An interface backlog item is complete only when populated, empty, busy, failure
and compact-window states are covered; an offscreen responsiveness test guards
the UI thread; keyboard and accessibility metadata are verified; and the
operator-facing behavior is recorded in the relevant durable documentation.
