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
9. Before playback, use the shared `Decision context` card to name the task,
   speaker in the game, synthesis voice, reference state, backend, model,
   profile, controls and exact decision effect. A blinded or unavailable field
   must say why it is hidden or unknown; it must not silently disappear.

Daily application and setup surfaces use the same status, busy, recovery,
accessibility and compact-layout conventions, but do not need the review ledger.

Blocking UI operations use `LatestTaskRunner` as the shared Qt worker boundary.
Each independent operation lane owns one runner, so a newer launch invalidates
only its older result while replay, decision, checkpoint and projection work
remain independent. The cohort reviewer, failure-reference audit and workbench
specialist launcher do not maintain private `QRunnable`/signal copies.

## Decision-context implementation, 2026-08-28

The specialist cohort, missing-voice/failed-line, failed-reference audit,
source-reference quality, generic blind A/B and terminal-conflict reviewers now
share one compact context component and canonical field names. Operator facts
stay above playback, while exact model paths, workspace IDs, plan/bundle IDs and
other provenance stay in the collapsed technical disclosure.

New missing-voice/failed-line review bundles publish and validate the common
speaker, synthesis voice, reference set, backend, model, profile, seed and repair
hypothesis as part of the immutable bundle. A one-candidate repair therefore
reveals the useful identity; a multi-candidate comparison publishes only values
shared by every arm and explicitly labels differing values as hidden. New
source-reference quality cards likewise capture backend, model, profile and seed
from their validated generation state. Older bundles remain readable and show
`Unknown (legacy review format)` instead of consulting mutable current settings.

The generic model comparison and terminal conflict interfaces intentionally
keep candidate-specific synthesis identity hidden until the blind decision gate.
They still show the line identity, decision purpose and consequence, and say
that candidate metadata is hidden rather than leaving an unexplained blank.

## Surface matrix

| Surface and entry point | Primary operator task | Current strengths | Findings and required change | Priority |
| --- | --- | --- | --- | --- |
| Specialist cohort review, `vntts-review-bundle` | Hear a checksum-bound sample set and make one cohort decision | Dedicated current-sample card, overall/current progress, heard ledger, stable navigation, async replay/save, elapsed commit/checkpoint/preload timing, terminal confirmation, explicit multi-reason bad-sample and more-sample outcomes, accessibility and shortcuts; navigation stays live during playback, while `EndOfMedia` credits the immutable playback target and preserves the newer selection. A separate stable mixed action rejects only marked WAVs, approves only individually heard acceptable WAVs and carries unsampled siblings into an exact pending successor. Successful decisions preload the exact successor without a second reload, while exact decision and non-authoritative listening-observation checkpoints keep the immutable publication reopenable and `--status` exposes reconciled progress without Qt. Observation checkpoints including diagnostic defect reasons are coalesced in a background writer; closing waits for the latest snapshot without blocking replay or decisions. | Use as the reference implementation. Keep real-data compact, crash-window recovery, tamper, failure and long-save regressions; avoid reintroducing broad workspace inspection, the old Rhiannon-only flow or workbench-embedded cohort authority. | Reference |
| Authoring workbench, `vntts-authoring-workbench` | Inspect generation state, run selected generation and repair individual outcomes | Background authority projection, immutable replay buffer, CAS-bound individual review, visible reasons, filters, accessibility and safe close behavior; its only cohort action builds an exact bundle in the background and opens the modal specialist reviewer | Keep cohort playback and decisions exclusively in `vntts-review-bundle`. Further workbench simplification may reorganize references/history/technical details, but must not reintroduce cohort authority or weaken individual-item authority. | P2 |
| Blind listening, `vntts-listen` | Compare hidden A/B audio and record one blinded preference | Clear A/B labels, seek/play controls, explicit `Neither acceptable`, deterministic hidden-key/report validation, async authoritative save, truthful report-recovery phase and deferred close | One current-trial card now shows `Trial N of M`, exact anonymous line/text evidence, completed/remaining counts and a textual decision lock/ready/saving reason. Playback and all four verdicts have accessible names, descriptions, stable shortcuts and a compact 640 x 400 two-row decision layout. Blinding and session/key/report bytes are unchanged. | Complete |
| Terminal conflict review, `vntts-conflict-review` | Resolve two contradictory historical terminal authorities without guessing from age or filename | Deterministic authority-independent A/B order, background immutable-byte preparation, `EndOfMedia` heard credit, replay, async CAS-bound save and recoverable owner/PID/process-start progress lease | After both blind listens, labels reveal each exact historical authority and manifest consequence before the operator commits. The wire contract and UI both require exactly two distinct WAV candidates; Neither retains a non-publishable repair requirement. | Complete |
| Failure reference audit, `vntts-reference-audit` | Select source audio suitable for cloning a failed speaker group | The task explicitly distinguishes a reference from character/speech approval; single-candidate groups use a binary suitable/unsuitable decision and multi-candidate groups select one or none. Stable candidate/group progress, complete heard ledger, collapsed affected lines, checksum-bound source replay, blinded labels, async save and safe close remain. An affected phrase can be rendered on demand with exact workspace backend/model/profile and seed zero; model load/render/cancel stay off the Qt thread, source playback and decisions remain usable during generation, the result is immutable memory-only evidence, and it cannot write authoring or decision state. | Retain as a second shared-contract reference. Preview output is optional and non-authoritative; persisted heard/preview state must not reveal the private candidate mapping or become decision authority. | Reference |
| Source reference quality, `vntts-reference-review` | Judge original and generated evidence for one source reference | Exact portrait support, separate original/generated playback, explicit accept/reject/need-another decisions, async authoritative save, in-dialog retry and deferred close | Evidence authority is now explicit: finishing the original unlocks Reject/Need another; Accept additionally requires every published generated sample to finish. A persistent text reason shows exact heard/required counts. Missing portraits collapse to a short truthful placeholder, excluded results stay behind a counted technical toggle, and the selected generated sample gets separate kind/duration/text details. Background audio preparation is bound to the exact card, row, queue ID and digest; changing the generated row cancels the stale result instead of playing it under the new label. Stable shortcuts, accessibility metadata, empty and 700 x 500 compact states are covered. | Complete |
| Main dashboard and compact controller | Start/stop reading and understand current speech state | Textual state, current dialogue, source/voice/latency fields and scrollable short-window layout; Start/Stop live is the single primary action, Read once is its explicit alternative, playback and emergency controls are grouped separately, and setup/support remains available during failure | Full and compact modes expose a persistent adjacent availability/recovery reason and accessible descriptions. Keyboard activation, short height and 100%, 150% and 200% scaled-font layouts are covered. Preserve this hierarchy as runtime actions evolve. | Complete |
| Settings | Configure capture, OCR, audio routing, manifests and backend | Keyboard-accessible section navigation over one canonical form, scrolling grouped layout, dependent controls, standard Save/Cancel and a shared labelled Browse contract for all eight filesystem inputs | Validation reports every current error inline, Save focuses the first invalid control without modal-warning churn, and runtime-bound fields carry visible and accessible restart markers. Saved settings are applied through the generation-bound background controller lifecycle: status stays visible, overlapping controller actions are disabled and a blocked live reader cannot freeze Qt. A temporary `Cancel settings apply` menu action interrupts only the matching pre-commit live-reader wait; the saved configuration remains for restart, while a request after the commit point truthfully reports that apply is already completing. Tests cover path selection, cancellation, multiple simultaneous errors, event-loop responsiveness, keyboard correction and 100%, 150% and 200% scaled-font layouts. | Complete |
| Onboarding wizard | Produce a verified first OCR-to-speech setup | Cancellable stale-safe diagnostics, explicit calibration, guarded end-to-end test and safe test cancellation; every page now shows `Step N of 5`, and the single dense configuration page scrolls independently in compact windows | Configuration validation reports all current errors inline and focuses the first invalid control without modal-warning churn. Tests preserve the diagnostics/test lifecycle and cover keyboard progress plus 100%, 150% and 200% scaled-font layouts. | Complete |
| Readiness | Verify the complete runtime chain and open remediation | Background stale-safe checks, cancel/retry, textual OK/warning/error status and a scrollable result table; diagnostics carry an explicit remediation identity, the first actionable error is selected automatically, and one contextual action explains both its enabled and disabled states | Keep new diagnostics on the typed remediation boundary instead of matching names or message text. Tests cover populated, ready, busy, cancelled, failed, compact, keyboard and accessibility states. | Complete |
| Model and voice assets | Download/verify a model and import a voice pack | Download, checksum verification, voice-pack import, character-reference import and active-manifest validation run in the background with visible progress, safe close and retry | The active manifest uses a labelled field with Browse and explicit checksum Validate actions. Validation is bound to the exact resolved path and manifest digest; changing either cancels the UI authority of stale work. Only Save and duplicate Validate are disabled while hashing, so the path and Cancel remain usable. Empty, changed, valid and invalid states remain inline, and Save waits for the exact current validation without modal churn. After Save, runtime application shares the generation-bound background controller lifecycle and request-scoped Cancel behavior with Settings, so a blocked live reader does not freeze Qt and overlapping controller actions remain unavailable until completion. Keyboard, accessibility, cancellation, event-loop and 680 x 440 compact states are covered without changing import ownership. | Complete |
| Voice preview and assignment | Preview a voice and assign or restore routing | Preview synthesis is asynchronous, exact target/candidate/text identity remains visible, mutable routing controls freeze during playback, request-scoped Stop reaches the loaded backend, close is deferred and Narrator routing differences are explained; entering the modal uses an asynchronous live-capture stop barrier instead of waiting on the Qt thread | Assignment remains deliberately independent from preview evidence: after playback ends the operator may change the candidate before saving. If assignment persistence becomes measurably slow, move it to the shared authoritative task lifecycle without making preview completion authorize it. | P3 |
| OCR uncertain-sample review | Correct or resolve captured low-confidence OCR evidence | Side-by-side screenshot, detected/corrected result, global/profile scope and background authoritative correction/resolution writes with retry and safe close; the queue now shows pending and `Current N of M` progress | `Resolve without correction` uses a non-blocking two-step confirmation that explains no reusable rule will be saved; changing samples resets that confirmation. Preserve this distinction from `Save correction and resolve`. | Complete |
| OCR correction editor | Maintain global and profile replacement rules | Clear scope tabs, editable two-column rules, background Save with retry/deferred close and standard Save/Cancel semantics | Global and profile row errors are reported together and marked on the exact cells; Insert adds a row and Control+Delete removes selected rows. Dirty Cancel/close requires an explicit second `Discard changes`. Search/import/export remains optional until real rule sets justify it. | Complete |
| Dialogue history | Search, replay and export the current session | Live search, readable labelled detail, text/JSON export and async replay with progress, retry and explicit Stop/Skip; search and selected details expose explicit accessible names and label buddies; entering the modal uses an asynchronous live-capture stop barrier | Timer refresh preserves the exact older selection, details and scroll position while new entries arrive, without an intermediate deselection. Closing during replay requests backend cancellation off the Qt thread and closes once stop is confirmed, even if the abandoned result future remains unresponsive. | Complete |
| Live diagnostics | Inspect the latest frame, OCR, routing and latency | Full capture preview, textual metrics, permission warnings, deliberate self-concealment during capture and a bounded refresh token that always returns to Retry after timeout/failure | A future enhancement may offer cancellation when the capture boundary itself has a safe request-scoped cancellation token; do not cancel shared live capture by inference. | P3 |
| Support and logs | Inspect redacted runtime events and export support evidence | Privacy-safe explanation, separate help/log tabs, direct support actions, preserved manual selection/scroll, explicit new-event navigation and in-dialog export progress/cancellation/failure/retry | Live diagnostics and Open settings folder now share Export's initiating-window lifecycle: only the active launcher disables, open/refusal/exception results return locally and the action restores for retry. Stable shortcuts, accessible metadata and a 620 x 420 compact state are covered without moving log selection or weakening redaction. | Complete |
| Game profiles | Create, duplicate, rename, remove and activate a profile | Small focused dialog with explicit active versus selected state, one primary `Use selected profile` action, a separate management group, destructive confirmation and prevention of active-profile removal; controller shutdown/start runs in one generation-bound background lifecycle. Every controller-mutating and modal-launch action is disabled until it finishes; Quit invalidates the generation and a late startup is immediately shut down instead of resurrecting the runtime. | A later visual pass may add profile icons or richer metadata, but activation and management semantics are now explicit without relying on color. | P3 |
| Calibration overlay and review | Select the full dialogue region and verify OCR | Frozen screenshot appears immediately, normalized selection, background OCR progress, clear draw/retry language and explicit `Save region without OCR preview` fallback | Mouse selection remains direct. Keyboard-only use starts a suggested lower-screen region with Enter, moves it with arrows, resizes it with Shift plus arrows, reviews with Enter and clears with R. The overlay and frozen evidence/actions expose accessible descriptions and shortcuts. Negative monitor origins and a physical screenshot at twice the logical overlay resolution preserve the same normalized crop. | Complete |
| macOS permissions | Understand and request screen/accessibility permissions | Text status, direct Request/Open Settings actions and restart guidance | Each permission row now owns its accessible status, request busy/error result and Settings-open result. Window activation refreshes status after a successful System Settings launch; synchronous native request calls remain explicitly synchronous rather than pretending to offer cancellation. Provider and launch failures remain local to the dialog without modal churn. | Complete |

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
