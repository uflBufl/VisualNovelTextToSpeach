# Specialist tools UI/UX evidence report

Reviewed 2026-09-03. This is evidence for synthesis, not an implementation
proposal. No UI code was changed.

## Scope and evidence

Covered every assigned specialist surface and its shipped transient dialogs:

- `AuthoringWorkbenchDialog` and all six `DisclosureSection` hosts;
- `CohortReviewBundleDialog`;
- `MissingVoiceReuseReviewDialog` in missing-voice and failed-line modes;
- `FailureReferenceAuditDialog`;
- `SourceReferenceQualityDialog`;
- `ModelListeningDialog`, including `SeekSlider`;
- `TerminalConflictReviewDialog`;
- shared `ReviewDecisionContext` in each host above;
- `Reverse1999AuditionDialog`;
- `StoryVoiceReviewDialog`;
- the Close controls, fatal-open messages and decision confirmations created by
  those routes.

Evidence sources:

- implementation and related tests in both repositories;
- offscreen execution of 110 authoring UI tests: all passed in 10.392 seconds;
- offscreen execution of 14 extractor UI tests: all passed in 0.202 seconds;
- one focused offscreen state-transition check reproduced stale audition state:
  displayed media 43 retained reviewed clip 42, Import stayed enabled after a
  disqualifying review-field edit, and an unnamed row retained speaker `Named`;
- static reconciliation of all Qt input, selection, action, disclosure,
  shortcut and standard-dialog construction sites in the ten UI modules.

The authoring run emitted only expected headless Qt Multimedia/CoreAudio
diagnostics. The extractor run emitted no test failures. The first extractor
test invocation used an invalid dotted module name because the repository
directory contains a hyphen; rerunning from that repository with `PYTHONPATH=.`
passed all 14 tests.

Judgment labels used below:

- **Verified**: behavior follows directly from code or a passing focused test.
- **Invariant**: the current behavior contradicts the interface's own stated
  rule or the review plan's accessibility/safety gate.
- **Heuristic**: design judgment still needing rendered or user evidence.

Dispositions use the shared vocabulary: keep, rename, move, merge, automate,
native, remove or add.

## Surface coverage and control accounting

### 1. Authoring workbench

Primary mission: open one validated workspace, review individual WAVs and run
collection-scoped generation. The existing hierarchy is explicitly
review-first; generation and secondary evidence live in the lower inspector.

| Controls, in current visual groups | Evidence and decision |
| --- | --- |
| Review filters: `review_character`, `review_status`, `review_collection`, `review_search`, `Narrator only`, `Characters only` | **Verified:** all filter independently from generation scope and persist where appropriate. **Merge:** `Narrator only` and the checkable `Characters only` duplicate states already expressible by a speaker filter; represent narrator/all-except-narrator in one clearly labelled filter. The six-control unlabelled row is dense at 900 px. |
| `review_table` row selection and horizontal/vertical scrolling | **Keep**, but **move** `Technical` and `Queue ID` behind an optional technical column view. Nine always-present columns produce horizontal scanning at the minimum width. Row editing and sorting are correctly disabled. |
| `Previous pending`, `Next pending`, `Replay`, `Stop selected audio`, `Approve`, `Reject` | **Keep. Verified:** exact WAV must finish before decisions; saves are non-blocking, fail closed and advance safely. **Rename:** remove shortcut text from `Approve (Ctrl+Enter)`, `Reject (Ctrl+Backspace)` and `Replay (Ctrl+R)`; expose shortcuts consistently in help/accessible descriptions instead of mixing them into only three labels. |
| `Refresh authority` | **Move/rename:** polling already refreshes on authority changes, so a permanent review-row action is implementation jargon. Show `Reload workspace` beside a blocked/stale status, and keep a manual refresh only in technical details if it still has a diagnosed use. |
| `Specialist cohort review` disclosure and `Open specialist cohort reviewer` | **Keep. Verified:** bundle building is asynchronous, retryable, modal, checksum-bound and refreshes the workbench after close. The section correctly explains that cohort authority belongs elsewhere. |
| Vertical `QSplitter`, inspector scroll area and scrollbars | **Keep. Verified:** the inspector remains reachable at 900x640 and remembers sizes. |
| `Reset layout` | **Move** into technical/recovery controls. It is currently the first inspector control even though it is low-frequency housekeeping. |
| `Outcome details` disclosure | **Keep** collapsed. Its source/fallback/skip counts are useful diagnosis but secondary to review. |
| `Generation scope and controls` disclosure; per-collection tree checkboxes; `Retry failed`; `Generate ready lines`; `Stop generation`; `Open output folder` | **Keep. Verified:** checked collections persist and produce exact queue IDs; retry cannot widen into unfiltered generation; stop escalates safely; output is revalidated on click. **Rename:** `Generate ready lines` should include the selected count already known by the projection. Keep `Retry failed` disabled reason visible, not tooltip-only. |
| `Readiness details` disclosure | **Keep** collapsed; the snapshot hashes and history are specialist evidence. |
| `Voice references` disclosure; editable `Recent previews`; `voice_search`; `voice_character`; `Previous reference`; `Play reference`; `Stop reference`; `Next reference` | **Keep** as preview-only evidence, but **rename** the section `Preview voice references` so it cannot be mistaken for workspace configuration. **Merge/clarify:** the two search inputs have different scopes but look redundant; label one `Recent preview` and the other `All configured voices`, or remove editable search from the eight-item recent list. |
| `Technical details` disclosure; read-only process log; `Copy diagnostics` | **Keep** collapsed. Diagnostics copy and UTF-8 log behavior are tested. |
| Six disclosure headers: outcome, specialist, generation, readiness, voice, technical | Component behavior is keyboard accessible and stateful. **Invariant:** the hand-written focus chain does not follow the rendered inspector order: it reaches collection controls before the generation header, reaches the voice contents before the voice header, and orders outcome/generation inconsistently. **Move** tab-order nodes to header-first visual order. |
| System close | **Keep. Verified:** close defers during authority work and asks before stopping a running generation child. |
| Native `Generation is still running` Yes/No question | **Keep/native. Verified:** `No` is the safe default. |
| Fatal `Unable to open authoring workbench` message | **Keep/native.** |

Accessibility accounting: every workbench `QPushButton` has an explicit name
and description, announcement labels emit accessibility events, and major
selectors have names. The unresolved defect is focus order, not missing button
metadata. Visible filter labels are still absent and large-text behavior is not
covered.

### 2. Specialist cohort review

Primary mission: hear the required sample set and commit one cohort-scoped
authority decision.

| Controls | Evidence and decision |
| --- | --- |
| `cohort_choice` | **Keep.** It states how many sample WAVs decide how many target WAVs. |
| `Show technical details` | **Keep** off by default. It reveals Quality and Line columns plus cohort audit data. |
| `table` row selection, double-click playback, scrollbars and resizable headers | **Keep. Verified:** single selection, non-editable rows, Space playback and Left/Right navigation work at 900x820. |
| `Previous sample`, `Play/Replay selected sample`, `Stop sample`, `Next sample` | **Keep. Verified:** playback uses immutable bytes and only end-of-media marks heard. |
| Seven defect checkboxes: `Pause or pacing`, `Repeated words or phrases`, `Truncated or missing words`, `Pronunciation or wrong words`, `Timbre or audio artifact`, `Wrong speaker or voice identity`, `Other or unclear defect` | **Keep.** These are the actionable repair taxonomy. |
| `Mark bad: other or unclear` / `Clear selected defect reasons` | **Keep** as the B-key fast path, but **rename** the clear state to `Clear all defect reasons` so its scope is explicit. It is a shortcut over the seven reason boxes, not a separate verdict. |
| `Need more evidence` | **Keep. Verified:** only enabled after all current samples and while a clean unsampled candidate remains below the five-sample cap. |
| `Leave undecided` | **Keep:** unlike an unexplained window close, it states the domain outcome and checkpoints observations. |
| Dynamic `Repair N marked; accept M heard; leave U pending` | **Keep. Verified:** the confirmation and projection preserve the mixed scope. The long dynamic label needs a large-text layout check. |
| Dynamic `Accept all N WAVs`, `Reject all N WAVs` | **Keep. Verified:** broad consequences are explicit and require a native confirmation. |
| Hidden `Retry bundle load` | **Keep/move** beside the blocked status. **Invariant:** `_update_actions` shows it whenever `samples` is empty, including successful completion with zero cohorts; `_update_operation_status` then falsely says authority could not be projected. Visibility must follow an explicit load/refresh error, never an empty-success state. |
| Review scroll area | **Keep.** It preserves a fixed decision region while allowing evidence to scroll. |
| System close | **Keep. Verified:** authority and checkpoint work defer closing. |
| Native confirmations for accept-all, reject-all, repair-marked and more-evidence | **Keep/native. Verified:** each names exact counts and defaults to No. |
| Fatal `Unable to open review bundle` message | **Keep/native.** |

**Invariant:** focus order is implicit creation order. Defect checkboxes are
created after every decision button even though they appear above those buttons,
so keyboard order does not match visual/decision order. Buttons also lack the
explicit accessible descriptions used by the workbench; tooltips are not a
sufficient screen-reader contract. **Add** one explicit focus chain and action
descriptions.

### 3. Missing-voice reuse / failed-line fallback review

Primary mission: hear every available opaque arm for each exact sample, then
choose one complete candidate or preserve the unresolved state.

Both shipped modes use the same controls:

| Controls | Evidence and decision |
| --- | --- |
| `Previous sample`, `sample_selector`, `Next sample` | **Keep.** The selector names length bucket and line ID; adjacent sample text provides the content. **Add** an accessible name to the selector and sample text. |
| One dynamic `Play/Replay <opaque label>` button per candidate, including disabled `<label> unavailable` arms | **Keep. Verified:** failed renders remain visible and cannot win; heard state is saved in the background. **Add** accessible names/descriptions. |
| `Stop audio` | **Keep**, but disable whenever no media is active as it already does after refresh. |
| One dynamic `Choose <label> for this family` or `Use fallback <label> for these lines` per complete candidate | **Keep. Verified:** incomplete candidates are hidden from decision controls and cannot win by omission. **Add** accessible metadata. |
| `Neither voice is acceptable` / `Keep these lines unresolved` | **Keep.** It is the necessary safe outcome. |
| Decision-context `Technical authority details` | **Keep** collapsed. |
| Standard `Close` | **Keep/native. Verified:** close waits for pending heard/decision writes. |

**Invariant:** failed-line mode changes the window, heading and decision labels,
but progress and save status still say `families`, `family decision` and
`complete opaque voice`. **Rename** every mode-sensitive status from the same
mode vocabulary so the failed-line workflow never claims to bind a family.

**Heuristic:** both playback and decision areas use unbounded horizontal columns.
The data format permits labels beyond Z and imposes no small candidate maximum;
`Ctrl+<column+1>` also stops being a usable shortcut scheme beyond nine arms.
**Add** wrapping/scrolling or a vertical candidate list, and assign shortcuts
only where representable. No test exercises more than two candidates.

There is no fatal-open dialog on this launch path: constructor failure escapes
the launcher. **Add/native:** catch the validated open error and show the same
actionable critical dialog pattern as the other authoring launchers.

### 4. Failed-reference audit

Primary mission: select suitable source evidence, not approve generated speech.

| Controls | Evidence and decision |
| --- | --- |
| `group_choice`, `Previous group`, `Next group` | **Keep.** The combo provides random access; previous/next and shortcuts support sequential work. |
| `candidate_choice`, `Play selected candidate`, `Stop` | **Keep. Verified:** source bytes are checksum-verified and all candidates must finish before either verdict. |
| `preview_text_choice`, `Generate voice sample`, `Replay generated sample`, `Cancel generation` | **Keep** as optional evidence, but **move** into a collapsed `Optional generated preview` disclosure. Four always-visible controls currently compete with the required source decision even though the copy says the preview is non-authoritative. Generation is correctly asynchronous and independently cancellable. |
| `Use selected candidate` / dynamic `Use Candidate N` / `Use this reference` | **Keep.** Single-candidate wording is tested and removes artificial ranking language. |
| `None of these references is suitable` / `This reference is unsuitable` | **Keep.** It correctly avoids rejecting the character. |
| `Show N affected failed line(s)` and read-only `cases` table | **Keep** collapsed. This technical detail is useful for consequence checking without dominating the task. |
| Decision-context `Technical authority details` | **Keep** collapsed. |
| Standard `Close` | **Keep/native. Verified:** closing defers during checksum, save or preview generation. |
| Fatal `Unable to open failed-reference audit` message | **Keep/native.** |

The explicit tab chain follows task order and the important controls have names
or descriptive visible text. **Move** the transient status from above the group
selector to the action/evidence region, or mirror it there; playback and save
outcomes currently appear far from the buttons that caused them. This is a
heuristic placement finding.

### 5. Source-reference quality review

Primary mission: decide whether one exact source reference is safe for cloning.

| Controls | Evidence and decision |
| --- | --- |
| `generated` list selection | **Keep.** It is hidden when no generated sample exists and has explicit accessible metadata. |
| `Play original reference`, `Play selected generated sample`, `Stop audio` | **Keep. Verified:** selected bytes are confined and checksum-verified; accept requires the original and all published generated samples, while reject/another-sample require the original. **Invariant:** `Stop audio` is never disabled when idle. Make its enabled state follow active playback. |
| `Technical exclusions (N)` disclosure | **Keep** hidden when empty and collapsed otherwise. |
| `Accept reference`, `Reject reference`, `Need another sample` | **Keep.** Availability is explained in a visible evidence-progress label and accessible descriptions. |
| Decision-context `Technical authority details` | **Keep** collapsed. |
| Standard `Close` | **Keep/native. Verified:** close waits for the authoritative decision write. |
| Fatal `Unable to open source-reference review` message | **Keep/native.** |

The explicit tab chain is correct and the 700x500 compact layout has a focused
test. There is no scroll container, large-text test or completed-decision
history. See the shared recovery and scale findings below.

### 6. Blind model listening

Primary mission: compare two anonymous samples without learning their models.

| Controls | Evidence and decision |
| --- | --- |
| Read-only/selectable trial text | **Keep.** It is the exact common stimulus and is explicitly named for assistive technology. |
| `Play A`, `Play B` | **Keep. Verified:** anonymous labels, colors and shortcuts remain stable, and automatic A-then-B playback works. |
| `-5s`, clickable/keyboard `SeekSlider`, `+5s`, time display | **Keep only after first complete listening. Invariant:** the UI says both samples must play completely, but dragging or skipping near the end still lets end-of-media add the side to `completed_sides`; no listened-range coverage is tracked. Disable seek/skip until that side has completed once, or track coverage. |
| Dynamic `Stop` / `Continue` / `Start again` | **Rename:** the button is Pause/Resume/Replay, not Stop; the current initial label violates native player expectation. Keep the fixed width and accessible description. |
| `A is better`, `B is better`, `Both acceptable / no preference`, `Neither acceptable` | **Keep. Verified:** the four outcomes are distinct and persist asynchronously only after both sides complete. |
| Decision-context `Technical authority details` | **Keep** collapsed; it reveals trial IDs, not candidate identity. |
| Standard `Close` | **Keep/native. Verified:** close waits for preference/report writes. |
| Fatal `Unable to open listening workbench` message | **Keep/native.** |

This is the strongest accessibility implementation in the specialist set after
the workbench: named controls, explicit tab order, non-color labels and a tested
640x400 state. The compact test checks the decision button, not the status and
Close rows, and no large-text variant is covered.

### 7. Terminal conflict review

Primary mission: hear both blind terminal WAVs, then preserve one historical
authority or require repair.

| Controls | Evidence and decision |
| --- | --- |
| `Play candidate A`, `Play candidate B` | **Keep. Verified:** candidates are deterministically blinded and copied bytes are rehashed before playback. |
| `Stop audio` | **Keep for active preparation/playback. Invariant:** `_set_actions(True)` enables it while idle, so its visible state falsely implies active audio. |
| Dynamic `Choose candidate A/B`, then `Keep Approved/Rejected candidate A/B` | **Keep. Verified:** authority is revealed only after both candidates finish. The post-listening consequence is explicit. |
| `Neither candidate is acceptable` | **Keep.** It maps to repair rather than silently selecting a losing authority. |
| Decision-context `Technical authority details` | **Keep** collapsed. |
| Standard `Close` | **Keep/native. Verified:** decision saves are asynchronous and close waits for them. |

The main launcher prints validation failure to stderr instead of showing a GUI
fatal-open dialog. For a desktop entry route this is an owned dead end.
**Add/native:** show `Unable to open terminal conflict review` consistently.

No explicit focus chain or complete accessible descriptions are present. The
creation order is close to visual order, but this remains unverified with a
screen reader.

### 8. Shared `ReviewDecisionContext`

Interactive control: one checkable `Technical authority details` tool button.
Four always-visible summary labels explain purpose, identity/reference,
synthesis and consequence.

**Keep** the component. It fixes a real cross-tool problem: the operator can see
what is being judged and what the decision changes. Every authoring host supplies
truthful fallback values rather than guessing legacy metadata.

**Merge/rename heuristics for primary integration:**

- Host-specific technical disclosures can sit immediately beside the shared
  technical toggle, producing two adjacent concepts called technical details.
  Use `Decision provenance` for the shared authority disclosure and reserve
  `Technical diagnostics` for playback/generation failures.
- Use the same chevron treatment as `DisclosureSection`; the shared tool button
  is checkable but does not set an arrow, so collapsed state is less obvious.
- Keep it near the decision, but consider a compact two-line summary on repeated
  cards. Four full-width lines plus a host heading dominate small dialogs.

These are heuristic refinements; the data and default-collapsed technical body
are correct.

### 9. Reverse: 1999 voice mapping manager

This window currently owns three missions at once: find/map a speaker, review a
clip, and import an approved reference.

| Controls | Evidence and decision |
| --- | --- |
| `search` and `chapter` filter | **Keep.** Search spans speaker, NPC ID and dialogue. **Add** visible/accessible field associations instead of anonymous `Find`/`Chapter` labels. |
| `dialogue` table selection and resizable splitter | **Keep.** It is the source evidence for identity. |
| `banks` list and unlabelled `media` combo | **Keep. Rename/label:** `media` needs visible `Media clip` and accessible metadata. |
| `Play selected clip`, `Stop` | **Keep. Add:** busy progress and cancellation while extraction/conversion/analysis runs. `play_clip` currently performs all work synchronously on the GUI thread. Disable Stop when idle. |
| Review fields `Music / SFX`, `Speakers`, `Speaker identity`; `Save clip review` | **Keep.** The explicit `Not reviewed` defaults are safe. **Invariant:** starting playback is enough to enable a save path; reaching the end is not recorded. Require complete listening before approval. |
| `Import reviewed clip as character voice` | **Keep**, but bind it to the exact displayed review token and warn before replacing an existing destination/manifest entry. It is correctly disabled until an approved review in the initial path. |
| `speaker_name`, `npc_id`, `Save local speaker mapping` | **Keep**, but **move** next to selected dialogue/bank identity and clearly separate `Save mapping` from `Import voice`. Both fields need real labels/buddies and validation feedback before click. |
| Standard `Close` | **Keep/native.** |
| Fatal `Unable to open speaker audition` message | **Keep/native.** |

**P0 invariant, reproduced offscreen:** selection-dependent state is not
invalidated consistently.

- Selecting an unnamed dialogue row does not clear the previous
  `speaker_name`; `refresh_dialogue` also leaves both identity fields intact.
- Changing `media` has no change handler, so `current_clip`, quality and review
  can still refer to the previously played media while the combo displays a new
  media ID.
- Changing any of the three review fields after approval does not clear
  `current_review` or disable Import.
- Import uses the current `speaker_name` but the old `current_clip` and old
  `current_review`; this can silently import under an identity that was not the
  one recorded with the approval.

The focused tests prove the happy path, but do not exercise any change after
playback or approval. **Automate:** centralize one `selection changed -> stop,
clear clip/review, disable import` transition for dialogue, bank and media;
centralize `review field changed -> invalidate approval`; snapshot and display
the exact speaker/media/review identity used by Import.

Accessibility metadata is nearly absent: inputs, lists, tables, status and
buttons have no explicit names/descriptions, there is no tab-order check, and
status changes are not announced.

### 10. Character Story voice reference review

Primary mission: inspect portrait/text/audio evidence and decide, defer or
compare references.

| Controls | Evidence and decision |
| --- | --- |
| `search`, `decision_filter`, `evidence_filter`, `Recommended first pass only` | **Keep.** They expose both human and automatic-evidence triage. **Add** visible and accessible labels for both combos; only the search has a visible `Find` label. |
| `table` selection, resizable headers and scrollbars | **Keep. Verified:** rows are read-only, evidence filters are truthful and candidate identity is checksum-bound. |
| `notes` line edit | **Keep**, but add a persistent visible label explaining `Saved with the next decision`. Placeholder-only labeling disappears while typing; changing selection discards unsaved text without warning. |
| `Previous pending`, `Next pending` | **Keep.** Pending navigation respects active filters. |
| `Play`, `Stop` | **Keep. Invariant:** Stop stays enabled while idle. More importantly, decision buttons are enabled before any audio finishes. |
| `Accept`, `Reject`, `Uncertain` | **Keep. Invariant:** an operator can accept a source reference without playing it at all. Other source/reference review tools require end-of-media evidence; this surface only revalidates checksum at save. Add per-candidate heard evidence and a visible disabled reason. |
| `Set current as A`, `Play A`, `Set current as B`, `Play B`, A/B labels | **Keep only if same-character comparison is the intended mission. Add:** prevent the same candidate in both slots, offer Clear, and warn/block cross-character comparison unless it has a defined meaning. `Play A/B` currently starts enabled with empty slots and merely reports the error after click. |
| System close | **Keep.** Playback is stopped safely. |
| Fatal `Character Story voice review` message | **Keep/native.** |

All decisions and file checks are synchronous on the GUI thread. References are
normally short, but there is no heartbeat/busy test for slow storage or a slow
decision write. **Move** file read/hash and save to the existing background-task
pattern if profiling or a slow-storage fixture reproduces a visible stall; do
not add concurrency speculatively.

Except for the portrait, interactive controls lack explicit accessible names
and descriptions, no focus-order test exists, and the fixed 1180x720 layout has
no scroll container or scale coverage.

## Ranked findings

| Priority | Finding | Evidence type | Smallest coherent correction |
| --- | --- | --- | --- |
| P0 | Voice mapping manager retains stale speaker/media/review state and can import a clip under a displayed identity that was never approved with it. | **Invariant, verified from event wiring and data used by save/import.** | Invalidate dependent state on every upstream change; gate Import on one immutable displayed review identity. Add one change-after-approval regression test. |
| P1 | Listening authority is inconsistent: Story review can decide without playback, audition can approve after playback merely starts, and blind listening seek/skip can satisfy a claimed full-listen gate without full coverage. | **Invariant.** Other authoring tools and their tests require end-of-media. | Share the rule, not necessarily a component: first complete playback authorizes; seeking becomes available afterward. Show a visible disabled reason. |
| P1 | Linear one-way tools offer no in-UI recovery from an accidental saved decision. Missing-voice, source-quality, listening and terminal-conflict immediately advance and expose neither history nor undo. | **Verified dead end.** | Add a checksum-revalidated `Review previous/Undo last decision` path where the underlying authority model permits it; otherwise add a pre-save confirmation only for irreversible decisions. |
| P1 | Accessibility quality changes abruptly at repository/surface boundaries. Workbench/listening/source-quality are named and partly ordered; extractor tools, missing-voice and terminal review are mostly unnamed. Cohort focus order contradicts layout; workbench focus order enters disclosure contents before headers. | **Invariant against the plan's keyboard/screen-reader gate.** | Add explicit names/descriptions, label buddies and visual-order focus chains; use one automated metadata/focus smoke test per window. |
| P1 | Large-text and small-screen support is unverified and structurally risky. Seven specialist dialogs have no scroll container; candidate buttons are unbounded horizontal rows; extractor minima are 1000x650 and 1180x720. | **Heuristic supported by layout structure; no 150%/200% evidence exists.** | Add scroll/wrapping only where the 150%/200% render fails. First test missing-voice dynamic arms and both extractor windows. |
| P1 | Audition extraction/conversion/quality analysis runs synchronously and has no busy/cancel state. | **Verified from `play_clip`; test gap for responsiveness.** | Reuse the repository's existing background runner pattern around prepare/analyze and expose one cancellable progress state. |
| P1 | GUI open errors are inconsistent: missing-voice constructor failures escape; terminal-conflict reports to stderr. | **Verified.** | Use native critical open dialogs with actionable path/error text on both routes. |
| P2 | Failed-line fallback mode leaks missing-voice `family` terminology. | **Verified copy/state defect.** | Derive all headings/status/decision text from the existing mode flag. |
| P2 | Idle Stop controls remain enabled in source-quality, terminal-conflict and both extractor windows; blind-listening's Stop is actually Pause/Resume/Replay. | **Verified.** | Tie enabled state and label to actual playback state; use native player verbs. |
| P2 | Successful completion of the last cohort can show `Retry bundle load` and a false projection-failure message because retry visibility is derived from an empty sample list. | **Invariant, verified from `_update_actions` and `_update_operation_status`.** | Track explicit load error state; render a clean completed state when zero cohorts remain. |
| P2 | Workbench has redundant narrator filters, an always-visible authority-refresh escape hatch and technical table columns in the primary view. | **Heuristic duplication/information hierarchy.** | Merge speaker filter states, move reload to recovery/technical context, hide technical columns by default. |
| P2 | Optional generated-preview controls dominate the failed-reference audit's required source decision. | **Heuristic.** | Put the four preview controls in one collapsed disclosure; keep all functionality. |
| P2 | `ReviewDecisionContext` and host-specific disclosures both use generic `technical details` vocabulary. | **Heuristic cross-surface consistency.** | Rename shared content `Decision provenance`; host failures remain `Technical diagnostics`. |
| P3 | Shortcut presentation is inconsistent: embedded in workbench labels, separate help in cohort, invisible elsewhere. | **Verified consistency gap.** | Keep clean outcome labels; expose shortcuts via one help/tooltip/accessibility convention. |

## Missing-capability evidence

Only capabilities tied to a reproduced dead end are proposed:

1. **Recover or revise the last linear decision.** Saving in missing-voice,
   source-quality, blind-listening or terminal-conflict immediately removes the
   card from the UI. A mistaken click cannot be corrected without leaving the
   interface or editing artifacts externally.
2. **Exact review identity in the audition importer.** The UI has no durable
   on-screen statement tying speaker, bank, media and approval together, while
   those inputs can change independently.
3. **Busy/cancel feedback for audition preparation.** A conversion/analysis
   stall leaves the same enabled-looking window and no cancellation route.
4. **Visible and announced disabled reasons in under-described tools.** Disabled
   decision controls in missing-voice and terminal review have no per-control
   accessible description; extractor decisions often remain enabled instead of
   explaining prerequisites.
5. **Truthful large-text containment.** Current minima and horizontal rows have
   no evidence at 150%/200%; a scroll or wrapping mechanism is required only
   where the mandated render proves clipping.

Not proposed: per-window volume controls, custom media players, a new design
system, or a shared mega-review framework. System volume, native Qt controls and
the existing small components are sufficient until evidence says otherwise.

## Verified strengths to preserve

- Authoring decisions are generally checksum-bound, fail closed on changed
  files/state and keep the Qt event loop responsive during authoritative writes.
- Workbench generation/review scopes are explicitly independent; an empty
  collection selection cannot widen retry or hide review authority.
- Cohort review clearly distinguishes sample observations from whole-cohort and
  mixed authority decisions, with exact-count confirmations.
- Missing-voice failed arms remain visible, cannot be selected, and zero-choice
  cohorts resolve automatically without fake human confirmation.
- Source-quality accept/reject prerequisites are visible and exposed to
  assistive technology.
- Blind model listening never exposes candidate identity before the aggregate
  report and represents `neither` separately from a tie.
- Terminal conflicts reveal historical approved/rejected authority only after
  both blind samples finish.
- Story review revalidates report, reference and displayed portrait identities
  before saving.

## Test and visual gaps

1. No specialist test captures a screenshot or validates 150%/200% text scale,
   platform font metrics, Windows layout, high contrast or screen-reader output.
2. Compact tests cover workbench reachability, cohort row separation,
   source-quality tab adjacency and one blind-listening decision-button bound;
   they do not cover every bottom row, long localized text or dynamic candidate
   counts.
3. No test asserts workbench/cohort visual-order focus traversal. Existing
   tests only prove focus presence, metadata and one adjacency.
4. Missing-voice tests use at most two candidates and do not exercise labels
   after Z, shortcut exhaustion or horizontal overflow.
5. No extractor test asserts accessible names, descriptions, label buddies,
   tab order, disabled reasons, large text, empty results or save/import busy
   behavior.
6. Audition tests cover selection, successful review and successful import only;
   no test changes dialogue, bank, media, speaker or review fields after
   playback/approval.
7. Story tests intentionally prove decisions remain enabled during playback;
   none requires completed listening or checks empty A/B button states.
8. Open-failure dialogs are not exercised for missing-voice or terminal review.
9. No test validates recovery or revision after a decision has advanced a
   linear session.

## Cross-surface concerns for primary integration

- Decide one vocabulary for `source speaker`, `game speaker`, `voice target`,
  `effective voice`, `reference`, `candidate`, `sample`, `family`, `group` and
  `cohort`. Failed-line mode already demonstrates how leaked vocabulary changes
  the apparent decision scope.
- Adopt one playback evidence rule: complete first listen authorizes; replay and
  seeking are conveniences afterward. Exceptions must state why.
- Adopt one recovery rule for single-item irreversible decisions. Whole-cohort
  confirmation is already strong; individual linear tools need revise/undo or
  an explicit reason why revision is impossible.
- Standardize native opening failures, background-save close deferral, idle Stop
  state and accessible disabled explanations.
- Reuse `DisclosureSection` behavior/chevrons and distinguish `Decision
  provenance` from operational diagnostics.
- Keep workbench individual-line authority and cohort authority separate. The
  existing handoff is sound; simplify the duplicate filters/technical details
  around it rather than merging the decision engines.
- Treat the two extractor interfaces as the accessibility and state-model
  baseline gap. Port the authoring patterns selectively; do not introduce a new
  framework just to make them look alike.

## Completion status

Every assigned surface, dynamic control family and transient dialog has a
recorded disposition. Implementation remains intentionally untouched. P0/P1
items need primary-agent deduplication against the other surface reports before
becoming backlog entries.
