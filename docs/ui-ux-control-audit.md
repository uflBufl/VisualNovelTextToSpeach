# Control-level desktop UI/UX audit

Reviewed 2026-09-03. This document consolidates the parallel review defined in
[`ui-ux-review-plan.md`](ui-ux-review-plan.md). It supersedes the completeness
claim in [`ui-ux-audit.md`](ui-ux-audit.md), whose behavioral baseline predates
several shipped surfaces.

## Outcome

The interface already has strong safety foundations: authoritative writes are
usually asynchronous and stale-safe, high-consequence review data is
checksum-bound, blinding is preserved, and the dashboard/onboarding use clear
primary actions. The main problems are not a missing design system. They are:

1. two state/consequence defects that can associate or remove the wrong data;
2. inconsistent state rules across controls that perform the same operation;
3. advanced configuration and tray actions exposed too prominently;
4. layouts that do not survive large text or constrained screens;
5. accessibility and playback-evidence rules that weaken at specialist and
   repository boundaries.

The smallest coherent direction is to simplify navigation and share policies,
not build a new UI framework.

## Evidence and coverage

The audit covers 11 application launch routes, 31 application-designed
surfaces/components, three tray notification states, and every identified
native chooser, name prompt, confirmation and warning call site in `VisualNovelTextToSpeach` and
`reverse1999-extractor`. `vntts-artifacts` has no UI.

Parallel evidence:

- runtime/setup: 194 focused tests passed and 17 offscreen states inspected;
- daily tools: 188 focused tests passed plus recursive offscreen control and
  scale inspection;
- authoring: 110 focused UI tests passed;
- extractor: 14 focused UI tests passed and the stale-review defect was
  independently reproduced.

The app integration suite appears in both the runtime/setup and daily-tools
passes, so these are execution counts rather than a claimed unique-test total.
This evidence was collected before the implementation pass summarized below.

Detailed control accounting is retained in:

- `.codex/investigations/ui-ux/runtime-setup.md`;
- `.codex/investigations/ui-ux/daily-tools.md`;
- `.codex/investigations/ui-ux/specialist-tools.md`;
- `.codex/investigations/ui-ux/integration.md`.

## Target information architecture

### Ordinary application

- The full dashboard is the home for current dialogue, live-reading state,
  offline preparation and direct recovery.
- Compact controls are the in-game transport surface. They must provide the
  same essential actions and state rules as the dashboard.
- The tray is a short status and escape surface, not the complete application
  sitemap. Keep open controls, live/read actions, essential transport, short
  status and Quit at the first level. Group or move setup and maintenance.
- Readiness is the setup/recovery front door after first launch. The onboarding
  wizard remains the guided first-run/reconfiguration flow.
- Settings exposes ordinary capture, audio, playback and application behavior.
  Raw model/manifest/index/sequence paths and rollout controls belong behind one
  technical disclosure and should be derived from a verified game pack where
  possible.
- Support owns logs, support export, live diagnostics and the settings-folder
  escape hatch. Contextual errors may link directly to the relevant action.

### Specialist tools

- Workbench owns workspace overview, individual-line review and generation.
- Dedicated reviewers retain cohort/reference/comparison authority.
- Complete first playback authorizes a review decision. Seeking and skipping
  become conveniences only after one complete listen unless a surface states a
  stricter rule.
- `Decision provenance` names the evidence and authority being judged;
  `Technical diagnostics` explains operational failures.
- Linear decision tools must offer a safe revision path when the authority
  model permits it, or explicitly confirm an irreversible action.

## Ranked implementation backlog

### P0: protect identity and data

| ID | Finding | Required change | Acceptance gate |
| --- | --- | --- | --- |
| UI-001 | `Reverse1999AuditionDialog` can retain an approved clip/review while dialogue, bank, media, speaker or review fields change. Import can then use a displayed identity that was never approved with those bytes. | Centralize upstream-change invalidation: stop playback, clear clip/review, disable Import, and bind Import to one immutable displayed speaker/bank/media/review token. | Change each dependency after approval; Import remains disabled until the newly displayed identity is completely replayed/reviewed. The saved manifest references exactly that token. |
| UI-002 | Removing a game profile also deletes profile-scoped OCR corrections, but the confirmation names only the profile and offers no undo. | State the exact profile and correction count in `Remove profile and its OCR corrections`; make Cancel the safe default. Prefer recoverable deletion if it is cheap. | The confirmation names every deleted data class; Escape/default cannot delete; focused tests verify retained data on cancel and complete deletion only after explicit confirmation. |

### P1: repair broken and misleading journeys

| ID | Finding | Required change | Acceptance gate |
| --- | --- | --- | --- |
| UI-003 | Diagnostics refresh can time out while the window remains concealed; fresh image-less snapshots and successful refreshes can retain an old screenshot or warning. | Restore the window on every terminal refresh path and replace/clear all snapshot-scoped evidence atomically. | Timeout, failure and success always reveal the dialog; image absence and cleared warning cannot display stale evidence. |
| UI-004 | Calibration review `Draw again` and `Cancel` both return to selection; Cancel does not cancel calibration. | Keep `Draw again` for retry and make Cancel close the calibration flow. | Each action has a distinct tested result from standalone and onboarding entry points. |
| UI-005 | Tray auto advance can enable a state Settings disallows, while first-run setup hides that automatic key dispatch and its permission requirement. | Drive tray and Settings from one guarded auto-advance policy. Disclose the behavior and required permission in the recommended setup path before diagnostics. | Screen capture/manual-sequence modes cannot enable invalid auto advance; disabled reason is visible; first-run user explicitly sees the dispatch choice. |
| UI-006 | Closing the unknown-speaker prompt silently approves narrator fallback for the session; its mapping dialog also allows changing another target and then re-prompts for the original. | Make close/Escape cancel or pause. Label the affirmative scope and lock the initiating speaker in contextual mapping while retaining editable target in general management. | Close never grants fallback; assignment resolves the initiating speaker exactly once; general voice management remains editable. |
| UI-007 | Default Pocket users see an XTTS model-download action that cannot succeed without unrelated CPML acceptance. | Make Asset Manager backend-aware; hide irrelevant model download or show only choices applicable to the active engine. | Default Pocket setup has no XTTS primary action; every offered download can satisfy the active backend's readiness check. |
| UI-008 | Settings accepts an existing but structurally invalid game-pack file, closes, and only then reports apply failure. It also presents independent raw authority files as peers. | Reuse onboarding pack validation before Save; choose game pack as the ordinary authority and move/derive independent technical paths. | Invalid pack remains in the open dialog with focused inline error; a valid pack cannot silently conflict with ordinary hidden paths. |
| UI-009 | Playback evidence rules disagree: Character Story can decide without listening, source audition can approve when playback merely starts, and blind listening seek/skip can satisfy a claimed full-listen gate. | Require one complete initial playback before decision; enable seeking afterward. Explain disabled decisions visibly and accessibly. | Start, partial play and seek-to-end do not authorize; natural first completion does; replay/seek then remain usable. |
| UI-010 | Offline preparation, voice mapping, OCR review, diagnostics, support and most specialist/extractor windows overflow at large text; several lack scroll/reflow entirely. | Add scroll, wrapping or responsive stacking only where 150%/200% renders fail. Reuse the dashboard's scrollable pattern. | Every surface is fully reachable at its minimum supported work area and 100%, 150%, 200% text scale without horizontal loss of primary actions. |
| UI-031 | During potentially long offline generation, the selection area disappears and the window shows only a phase sentence plus Cancel. It exposes no live completed/total count, saved progress, failure/recovery count or clear final handoff into pack activation. | Keep one window and replace the hidden selection area with a compact progress card: current phase, completed/total, saved-so-far guarantee, failures being recovered, and exact cancel/resume consequence. Poll the existing durable generation state instead of adding a new event framework. Keep per-line detail hidden unless failures need action. | A slow synthetic generation visibly advances durable counts; cancellation reports what was saved and resumes without duplication; recovery, final checks, pack creation and activation have distinct truthful states; success shows the final original/prepared/live-fallback coverage summary. |
| UI-011 | Tray, dashboard and compact transport enable actions from controller readiness rather than actual pause/queue/history capability. Compact also omits Replay and labels emergency stop merely `Stop`. | Use one runtime capability state across all three surfaces; add compact Replay; use `Emergency stop` or an equally explicit compact label. | Action availability and explanations match across surfaces for idle, live, speaking, paused, queued and replayable states. |
| UI-012 | Reverse: 1999 audition prepares, converts and analyses clips synchronously on the Qt thread with no busy/cancel state. | Run the existing bounded operation outside the UI thread and expose request-scoped progress/cancel. | A slow-storage fixture leaves the window responsive, prevents conflicting edits and supports safe cancellation. |
| UI-013 | Accessibility quality drops sharply in extractor, missing-voice, cohort and terminal-conflict tools; workbench/cohort focus order conflicts with visual order. | Add names/descriptions, label buddies, visible disabled reasons and explicit visual-order focus chains; repair shared disclosure order. | One keyboard/accessibility smoke journey per window reaches evidence, decision, recovery and close in rendered order. |
| UI-014 | Linear reviewers advance immediately after saved decisions and offer no in-UI correction path. | Add checksum-revalidated `Review previous`/undo where the domain supports reversal; otherwise add an explicit irreversible confirmation only where consequence warrants it. | A mistaken decision has a documented, tested recovery path without manual artifact editing. |
| UI-015 | Missing-voice constructor failures escape and terminal-conflict open failures print only to stderr. | Use the existing native critical-open pattern with actionable path/error text. | Every desktop launch failure remains visible and recoverable without a terminal. |

### P2: simplify hierarchy and remove avoidable friction

| ID | Finding | Required change |
| --- | --- | --- |
| UI-016 | The tray has about thirty named actions, status/dialogue rows and conditional/transient actions in one flat menu; long OCR text can determine menu width. | Keep a short first level, group rare Playback/Setup/Support actions, truncate non-action text, and remove duplicates whose home is the dashboard/support surface. |
| UI-017 | `Setup and diagnostics`, `Run setup`, `Ready to play`, `Live diagnostics` and `Diagnostics and logs` describe different jobs with overlapping labels. | Use `Run setup`, `Check readiness`, `Live diagnostics`, and `Support and logs` for the four distinct missions. |
| UI-018 | User-facing terms drift: `dialog`/`dialogue`, `Full`, `control window`, `voice mapping`, `manifest`, `reference`, `prepare` and `generate`. | Use the vocabulary contract from the review plan; rename tray dialogue and compact/full navigation first. |
| UI-019 | Diagnostics explains permission/capture failures but does not route to existing remediation. | Expose one contextual typed remediation action; do not add a permanent button row. |
| UI-020 | Empty Dialogue History leaves `Replay selected` enabled as a silent no-op. | Initialize action state before the unchanged-empty early return. Rename `Stop / skip replay` to `Stop replay`. |
| UI-021 | OCR Review offers enabled Save with no edit, then opens a no-op information modal; the correction editor defaults to higher-blast-radius Global scope. | Disable/explain Save until content changes; select the active profile scope when available. |
| UI-022 | Voice force-live changes persist only when the assignment button is pressed; Close silently drops a changed checkbox. | Make routing state apply immediately or present an explicit Save boundary. |
| UI-023 | Workbench primary view contains redundant narrator filters, permanent authority refresh, technical columns and mixed shortcut text. | Merge narrator filter states, move reload/reset to recovery/technical context, hide Queue ID/Technical columns by default and keep shortcuts out of button labels. |
| UI-024 | Cohort completion can expose `Retry bundle load` and a false projection-failure message when zero cohorts remain. | Drive retry visibility from explicit load error and render a clean completed state. |
| UI-025 | Failed-reference optional generated-preview controls compete with the required source decision; shared and host disclosures both say technical details. | Collapse optional preview; call shared authority details `Decision provenance` and reserve `Technical diagnostics` for failures. |
| UI-026 | Idle Stop controls remain enabled in several reviewers/extractor tools; blind listening's Stop control is actually Pause/Resume/Replay. | Bind enabled state and verbs to actual playback state using normal player terminology. |
| UI-027 | Profile creation is labelled `New` although it clones current setup; activation summary omits major capture/content/audio changes. | Rename to `Save current setup as profile` and summarize meaningful deltas before activation. |
| UI-028 | Dashboard hides the detected speaker in Technical details even though speaker attribution is ordinary dialogue context. | Move speaker into the current-dialogue card; keep voice/source/confidence/latency technical. |
| UI-029 | Readiness clips the component name that must be fixed; granted macOS permissions continue to show Request/Open actions. | Size status/component columns before details; reduce granted permission rows to a quiet state plus only intentional management action. |
| UI-030 | Character Story A/B buttons start enabled with empty slots, allow duplicate/cross-character comparisons, and notes can be lost on selection change. | Gate Play A/B, prevent duplicate/undefined comparisons, add Clear, and protect labelled unsaved notes. |
| UI-033 | Background-mode, unmapped-speaker and critical-error tray notifications are owned transient UI. The unmapped-speaker state also opens a prompt, so it can duplicate attention and error notifications may expose unbounded text. | Define one notification policy: when a balloon adds value, bounded/privacy-safe text, whether activation opens the relevant recovery surface, and when a visible prompt suppresses the duplicate balloon. |

## Preserve these working contracts

- checksum and exact-byte validation before review/playback/authority writes;
- stale-result rejection and request-scoped cancellation;
- asynchronous authoritative writes with deferred close and local retry;
- explicit sample-level versus cohort/reference-level consequence;
- preserved blinding and separate `neither` outcomes;
- typed readiness remediation rather than matching display text;
- one dominant live-reading action and separated emergency action;
- native file choosers and standard controls.

Do not add a design-system dependency, custom file browser, custom media player,
mega-review framework, profile-icon system, OCR rule import/export or general
undo framework without new evidence.

## Implementation order

1. UI-001 and UI-002: identity/data safety.
2. UI-003 through UI-009: incorrect state, consequence and authorization.
3. UI-010 through UI-015 and UI-031: responsive access, generation progress,
   capability parity and recovery.
4. UI-016 through UI-022: ordinary-player information architecture.
5. UI-023 through UI-030: specialist efficiency and consistency.
6. UI-032 and UI-033: qualify distribution and notification behavior without
   replacing native platform UI.
7. Real macOS/Windows visual, keyboard and assistive-technology qualification.

Each implementation slice should fix the shared root boundary, include one
focused regression that fails before the change, and preserve the contracts
above.

## Implementation update

Implemented 2026-09-03:

- UI-001 through UI-031 and UI-033 are complete at the source, offscreen UI and
  automated-regression level across `VisualNovelTextToSpeach` and
  `reverse1999-extractor`.
- The changes preserve immutable/checksum-bound review evidence, add guarded
  natural-playback authorization, unify runtime control policy, expose durable
  offline-generation progress, simplify tray and specialist hierarchy, and add
  responsive/accessibility coverage at 150% and 200% text scale.
- Verification passed all 2,062 discovered `VisualNovelTextToSpeach` tests and
  all 21 extractor UI tests with the main UI runtime, plus Ruff and diff checks
  in both repositories.

The remaining work is platform and human qualification, not another source
inventory or UI redesign.

## Remaining validation gaps

The source/offscreen audit is complete, but final visual qualification still
requires:

- real Windows and Cocoa tray/native-dialog behavior, including notification
  activation and duplication;
- real Windows portable extraction and first-launch journeys;
- fullscreen and multi-monitor floating prompts/compact controls;
- VoiceOver and Windows Narrator journeys;
- physical 100%, 150% and 200% scaling with platform fonts;
- representative novice first-run and returning-player observation;
- specialist review with long text, large candidate counts and slow storage.

These gaps affect confidence and acceptance, not the two reproduced P0 defects
or the source-verified state inconsistencies above.
