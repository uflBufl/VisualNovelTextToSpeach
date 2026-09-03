# Control-level UI/UX review plan

## Goal

Produce an evidence-backed decision for every shipped desktop surface and every
interactive control in the three-repository VNTTS workspace. Each control must
end with one disposition: keep, rename, move, merge, replace with automatic or
native behavior, remove, or add a missing capability. The review must improve
ordinary player journeys before polishing specialist authoring tools.

This is a plan for the review, not a redesign. The existing
[`ui-ux-audit.md`](ui-ux-audit.md) remains the behavioral baseline; this pass
adds the missing control-by-control and cross-application evidence.

Execution status, 2026-09-03: the three parallel evidence scopes are complete
and consolidated in [`ui-ux-control-audit.md`](ui-ux-control-audit.md). Real
platform and representative-user validation remains open.

## Scope and baseline inventory

Include shipped PySide6 interfaces and distribution UI in:

- `VisualNovelTextToSpeach`;
- `reverse1999-extractor`;
- `vntts-artifacts`, which currently has no UI and therefore contributes no
  surfaces.

Also include the application's background-mode, unmapped-speaker and
critical-error tray notifications.

Exclude example/demo windows, notebooks, CLI-only commands and OS UI that the
applications do not control. Include every native file chooser, name prompt,
confirmation and warning call site because its wording and launch context are
part of the journey.

### Launch routes

| Group | Launch route | Primary purpose |
| --- | --- | --- |
| Player | `vntts-app` | Daily reading, setup, preparation and support shell |
| Player/setup | `vntts-calibrate` | Standalone capture-region calibration |
| Authoring | `vntts-authoring-workbench` | Generation and individual-result workbench |
| Authoring | `vntts-review-bundle` | Specialist cohort review |
| Authoring | `vntts-reference-audit` | Failed-reference selection |
| Authoring | `vntts-reference-review` | Source-reference quality review |
| Authoring | `vntts-listen` | Blind A/B model listening |
| Authoring | `vntts-conflict-review` | Terminal-authority conflict resolution |
| Authoring | `vntts-pregenerate missing-voice-reuse-review-ui` | Missing-voice and failed-line fallback review |
| Source extraction | `r1999-audition` | Source clip review and speaker mapping |
| Source extraction | `r1999-story-voice-review-ui` | Character Story reference review |
| Distribution | Windows portable bundle | Extract and launch |

### Surface groups

The initial source inventory contains 31 application-designed
surfaces/components and three tray notification states. Runtime inspection may
split stateful surfaces into more ledger rows.

| Group | Surfaces to review | Source |
| --- | --- | --- |
| Runtime navigation | Tray menu; full dashboard; compact controller; live-voice preflight prompt; unmapped-speaker prompt | `vntts/app.py`, `vntts/dashboard_ui.py` |
| First run and configuration | Onboarding welcome, configuration, diagnostics, calibration and end-to-end pages; Settings; Readiness; calibration overlay and review; Game profiles; Asset manager; Voice import; macOS permissions | `vntts/onboarding_ui.py`, `vntts/app.py`, `vntts/readiness_ui.py`, `vntts/calibration.py`, `vntts/profiles_ui.py`, `vntts/asset_ui.py`, `vntts/macos_ui.py` |
| Content and voice preparation | Offline audio preparation; embedded voice audition; narrator/character voice preview and assignment | `vntts/pregeneration_ui.py`, `vntts/pregeneration_audition_ui.py`, `vntts/voice_preview_ui.py` |
| Evidence, history and support | Uncertain OCR review; OCR correction editor; dialogue history; live diagnostics; support/log center | `vntts/ocr_review_ui.py`, `vntts/ocr_corrections_ui.py`, `vntts/history_ui.py`, `vntts/diagnostics_ui.py`, `vntts/support_ui.py` |
| Authoring and evidence decisions | Authoring workbench; cohort review; missing-voice/failed-line review; failed-reference audit; source-reference quality review; blind A/B listening; terminal conflict review | `vntts/authoring/*_ui.py` |
| Source extraction | Reverse: 1999 voice mapping manager; Character Story voice reference review | `reverse1999-extractor/r1999extractor/reverse1999_audition_ui.py`, `reverse1999-extractor/r1999extractor/story_voice_review_ui.py` |
| Distribution and notifications | Windows portable extraction/launch; background-mode, unmapped-speaker and critical-error notifications | `packaging/windows/README.md`, `vntts/app.py` |
| Shared/transient | Review decision-context disclosure; workbench disclosures; sequence selectors; profile-name prompts; file/folder/export choosers; confirmations; validation warnings and fatal-open errors | All UI modules above |

The shared decision-context and disclosure components are reviewed once as
components and again in every host surface. A good component can still be in
the wrong place in one workflow.

## Review artifacts

Keep temporary screenshots, observations and generated inventories under
`.codex/investigations/ui-ux/`. Publish only durable conclusions in `docs/`.

The review produces:

1. `surface-ledger.csv`: one row per window, page, prompt, tray menu and
   meaningful state.
2. `control-ledger.csv`: one row per interactive control and native-dialog call
   site.
3. `journey-ledger.md`: end-to-end observations for the required user missions.
4. `screenshots/`: named evidence for each required state, viewport and scale.
5. `ui-backlog.md`: deduplicated, ranked changes with evidence and acceptance
   gates.
6. An updated durable UI audit after decisions have been validated.

Do not open implementation tickets directly from raw notes. First merge
duplicate findings and decide the shared root change.

## Ledger schema

### Surface ledger

Record:

- stable surface ID, repository, launch route and parent surface;
- intended user, mission, frequency and consequence of error;
- entry points and exit paths;
- useful, empty, loading, partial, success, failure and recovery states;
- minimum tested viewport, 100%, 150% and 200% text scale;
- keyboard-only path, focus order and screen-reader summary;
- close, cancel, retry and stale-result behavior;
- screenshot/evidence links and reviewer.

### Control ledger

Record:

- stable control ID, surface/state, visible label, type and current position;
- value/default, enabled/hidden rules and adjacent explanation;
- user task and exact consequence;
- duplicates or competing entry points;
- keyboard shortcut, focus order, accessible name/description and label buddy;
- validation, error, busy, success, cancellation and undo behavior;
- evidence source and confidence;
- decision: keep, rename, move, merge, automate, native, remove or add;
- proposed position/wording and backlog link.

Rows are created from source and runtime introspection, not handwritten from
memory. Hidden and disabled controls still require rows.

## Control interrogation

Ask these questions for every button, field, selector, table action, tab,
disclosure and menu item:

1. What concrete user mission fails if this control disappears? If none, remove
   it or move it out of the ordinary path.
2. Is the application asking the user for information it can derive, remember
   or validate automatically?
3. Is this the first useful moment for the decision, or is it too early, too
   late or duplicated elsewhere?
4. Does the control belong beside the object it changes? Does visual and tab
   order match the user's decision order?
5. Is the control type native and predictable for the task? Prefer direct
   selection and platform behavior over custom interaction.
6. Does the label describe the outcome in the user's language? Eliminate vague
   labels such as generic Save, Fix, Full or Advanced where scope is unclear.
7. Is the safest/commonest value the default? Is a remembered value still valid
   in the current profile, story and model context?
8. When disabled or hidden, can the user understand why and what to do next
   without a tooltip or log?
9. Does activation show immediate progress, prevent accidental duplication,
   allow safe cancellation and end with clear success or recovery?
10. Can the task be completed with keyboard and assistive technology, at compact
    size and large text, without relying on color, position or hover?
11. Is visual weight proportional to frequency and risk? One primary action
    should dominate; emergency/destructive actions need separation, not noise.
12. Is another control solving the same root need? Fix the shared flow once
    instead of polishing duplicates.

For any proposed new control, ask the inverse: which observed dead end requires
it, and why automation, better defaults, inline explanation or an existing
control cannot solve that dead end?

## Required user missions

Review controls in journeys, not only as isolated screenshots.

### Ordinary player

1. First launch -> permissions -> game selection -> model/voice readiness ->
   calibration -> successful OCR-to-speech test.
2. Returning launch -> start live reading -> pause -> skip/replay -> stop.
3. Prepare selected story audio -> resolve only ambiguous voices -> recover from
   a failed generation -> activate the pack.
4. Encounter an unknown speaker -> choose a distinct voice or explicitly use
   narrator -> resume reading.
5. Diagnose no capture/no speech/wrong voice -> take the direct remediation ->
   verify recovery.
6. Correct uncertain OCR -> confirm scope -> verify the correction is reusable.
7. Find and replay history -> export it -> return to live reading.
8. Change profile/settings/assets -> understand when restart/application is
   pending -> cancel or recover safely.

### Authoring operator

1. Open a workspace -> understand readiness -> generate only eligible lines ->
   review an individual result.
2. Move from workbench to specialist cohort review without duplicated authority.
3. Review missing-voice reuse, failed references and source-reference quality
   while preserving blinding and checksum-bound evidence.
4. Resolve a terminal conflict and recover from a stale/tampered/failed save.
5. Complete blind A/B listening without learning candidate identity early.

### Source curator

1. Find an unmapped speaker -> audition ranked clips -> mark clip quality ->
   save mapping/import the approved reference.
2. Filter Character Story candidates -> inspect portrait/text/audio evidence ->
   decide or defer -> compare A/B references.

## Parallel execution contract

Evidence collection and first-pass control review run in parallel. Cross-app
decisions remain centralized so vocabulary, priorities and shared fixes do not
diverge.

| Owner | Scope | Private evidence file |
| --- | --- | --- |
| Runtime/setup agent | Tray, dashboard, compact controller, runtime prompts, onboarding, Settings, readiness, calibration, profiles, assets and permissions | `.codex/investigations/ui-ux/runtime-setup.md` |
| Daily-tools agent | Offline preparation, voice assignment, OCR review/corrections, history, diagnostics and support | `.codex/investigations/ui-ux/daily-tools.md` |
| Specialist agent | Authoring review tools and both `reverse1999-extractor` interfaces | `.codex/investigations/ui-ux/specialist-tools.md` |
| Primary agent | Inventory reconciliation, distribution and notification UI, shared components, duplicated concepts, terminology, journey integration and final backlog | Final ledgers and durable audit |

All agents must:

- use the same surface/control ledger fields and disposition vocabulary;
- inspect source, tests and truthful runtime/offscreen states rather than infer
  behavior from labels alone;
- account for every control in scope, including hidden, disabled, dynamic and
  transient controls;
- distinguish verified behavior, violated invariant and heuristic judgment;
- record cross-surface concerns without deciding them locally;
- avoid implementation changes and avoid editing shared final artifacts.

The primary agent reconciles the three evidence files, audits shared components
in every host context and performs one sequential comparison pass across:

- tray, dashboard and compact control duplication;
- onboarding, readiness, Settings and asset-management ownership;
- offline preparation, voice assignment and unknown-speaker recovery;
- workbench versus specialist-review authority;
- vocabulary, primary-action hierarchy and recovery behavior across all tools.

Gate: parallel work is complete only when each private scope has no unreviewed
surface or control. Final conclusions are not complete until the primary agent
has resolved duplicate findings and cross-app conflicts.

## Execution passes

### Pass 0: freeze the inventory

- Parse every UI module and entry point for windows, pages, actions, controls,
  standard dialogs and dynamic construction sites.
- Launch every route with its smallest truthful fixture and recursively inspect
  Qt children for text, geometry, visibility, enabled state, focus policy,
  shortcut and accessibility metadata.
- Reconcile source and runtime lists. Every mismatch becomes an explicit ledger
  row or exclusion with a reason.

Gate: all 11 application launch routes and the Windows portable first-launch
flow open or have a reproduced, owned blocker; every designed surface,
notification state and native-dialog call site has a stable ID.

### Pass 1: establish mission and information hierarchy

- Name one primary mission and one primary action for each surface.
- Mark user level: ordinary player, advanced player, authoring operator or
  source curator.
- Map repeated concepts and entry points across tray, dashboard, onboarding,
  readiness, Settings and assets.
- Identify controls whose presence exposes implementation detail rather than a
  user decision.

Gate: no surface has two competing primary missions without a written reason;
every control maps to a mission.

### Pass 2: capture state and layout evidence

- Render every surface with populated, empty, loading/busy, partial, failure and
  recovered data where the state exists.
- Exercise permissions denied, unavailable backend/model, offline download,
  validation errors, long lists/text, stale evidence and interrupted work.
- Capture minimum supported window size and 100%, 150% and 200% text scale on
  macOS and Windows. Include fullscreen-game and multi-monitor overlays where
  relevant.
- Record clipped content, unnecessary whitespace, unstable control movement,
  weak grouping, scrolling traps and default-button surprises.

Gate: each surface ledger row either has state evidence or a justified
not-applicable marker. No screenshot alone counts as interaction evidence.

### Pass 3: review every control

- Walk the control ledger in visual order, then keyboard order.
- Apply the twelve interrogation questions and record one disposition.
- Test the actual action, including blocked, busy, cancel, retry and completion
  behavior.
- Review copy in context, including confirmation and error text.
- Audit duplicated controls at their shared implementation boundary before
  recommending per-surface fixes.

Gate: no control row is unreviewed; delete/move/add decisions have evidence and
an expected journey effect.

### Pass 4: find missing capabilities

- Observe where users stop, guess, open another surface, consult documentation,
  repeat input, wait without confidence or cannot recover.
- Compare every journey step with its required information, action and feedback.
- Inspect telemetry/support logs only in privacy-safe aggregate for recurring
  dead ends.
- Prefer removing the need, deriving the value, improving a default or adding
  inline feedback before adding a button or field.

Gate: every proposed addition cites an observed or reproducible dead end. A
feature idea without evidence stays out of the backlog.

### Pass 5: synthesize one backlog

- Merge findings with the same root cause, especially tray/dashboard duplication,
  setup/settings overlap and repeated authoring review controls.
- Rank P0: unsafe, destructive or core mission blocked; P1: frequent ordinary
  journey failure; P2: recurring confusion, redundancy or accessibility gap;
  P3: specialist efficiency or visual polish.
- Tag confidence as observed user evidence, violated invariant or heuristic.
- State the smallest coherent fix and its acceptance evidence. Prefer deletion,
  native Qt behavior and shared boundaries over new components.

Gate: each item has owner, affected surfaces, evidence, acceptance criteria and
dependencies; no duplicate tickets remain.

### Pass 6: validate before implementation

- Prototype only uncertain P0/P1 information-architecture changes. Static
  annotated screenshots are enough unless interaction timing matters.
- Re-run the affected mission with representative novice and experienced users;
  measure task completion, wrong turns, recovery, time and confidence.
- Reject changes that only look cleaner but make evidence, scope, safety or
  accessibility less explicit.
- Convert validated items into implementation slices, each with its focused UI
  regression check.

Gate: every high-impact redesign has behavioral evidence; lower-impact changes
can rely on a clear invariant and focused regression test.

## Starting hypotheses to test

These are investigation leads, not accepted findings:

- The tray exposes roughly thirty actions and may duplicate too much of the full
  dashboard instead of acting as a compact status/escape surface.
- Settings has more than thirty static control construction sites and overlaps
  onboarding and asset management; ordinary and expert configuration may not be
  separated at the right boundary.
- `Setup and diagnostics`, `Run setup`, `Ready to play`, Settings and asset
  management may describe one recovery journey with inconsistent entry labels.
- The authoring workbench is intentionally broad; its remaining playback,
  individual decision and specialist-launch controls need a strict ownership
  check against the dedicated reviewers.
- The two extractor interfaces have functional tests but less explicit compact,
  large-text and accessibility coverage than the main application's reference
  review surfaces.
- `dialog`/`dialogue`, `voice`, `reference`, `mapping`, `routing`, `prepare` and
  `generate` may expose inconsistent vocabulary across player and authoring
  contexts.

## Final completion gate

The planning work is complete when:

- all launch routes, distribution UI, designed surfaces, embedded components,
  notifications and transient call sites are accounted for;
- every interactive control has a reviewed disposition and evidence level;
- every required mission has been run through success and recovery;
- keyboard, screen-reader, compact-window and large-text paths are covered;
- proposed missing capabilities come from demonstrated dead ends;
- one deduplicated backlog states what to remove, simplify, move, rename or add,
  in priority order, with testable acceptance criteria.
