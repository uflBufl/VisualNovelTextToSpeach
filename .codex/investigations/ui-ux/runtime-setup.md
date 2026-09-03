# Runtime and setup UI evidence review

## Scope, method, and confidence

Reviewed the implementation and focused tests for:

- the system-tray menu, `ControlDashboard`, and `CompactController`;
- sequence-position choosers, live-voice preflight, and unknown-speaker prompt;
- every `OnboardingWizard` page and its navigation;
- `SettingsDialog`, `ReadinessDialog`, calibration overlay/review;
- `GameProfilesDialog` and its name/delete/error prompts;
- `AssetManagerDialog`, `VoiceImportDialog`, and their file/error dialogs;
- `MacOSPermissionsDialog` and its application entry points.

Evidence sources:

- source inspection of `vntts/app.py`, `configuration_apply.py`,
  `durable_settings.py`, `dashboard_ui.py`, `onboarding_ui.py`,
  `readiness_ui.py`, `calibration.py`, `profiles_ui.py`, `asset_ui.py`, and
  `macos_ui.py`;
- all focused runtime/setup tests: **194 passed** offscreen in 2.212 seconds;
- recursive Qt widget inspection for enabled/hidden state, size hints, focusability,
  and accessible names;
- 17 offscreen renders at the intended default/minimum sizes, including useful,
  error, expanded, and disabled states.

Confidence tags used below:

- **Verified**: follows directly from implementation and a test or runtime probe.
- **Source-verified**: follows directly from implementation but lacks a runtime
  regression.
- **Heuristic**: a UI judgment requiring user validation before implementation.

Offscreen rendering does not validate real Cocoa/Windows tray behavior, native
file-dialog layout, VoiceOver/Narrator announcements, fullscreen games, or
physical high-DPI displays.

## Surface coverage

| ID | Surface/state coverage | Evidence |
| --- | --- | --- |
| RT-TRAY | startup, ready-state rules, conditional sequence actions, transient cancel-apply action, macOS-only permission action | Source, menu introspection, `test_app.py` |
| RT-DASH | default, expanded technical/setup disclosures, ready/unready, live/paused, sequence recovery, short and scaled window | Render, source, `test_dashboard_ui.py` |
| RT-COMPACT | ready/unready, live/paused/warning, conditional sequence action, scaled content | Render, source, `test_dashboard_ui.py` |
| RT-SEQ | zero/one/multiple expected candidates and full resync chooser | Source, `test_app.py` |
| RT-PREFLIGHT | multiple unresolved speakers, assign/narrator/cancel/close, stale recheck | Render, source, `test_app.py` |
| RT-UNKNOWN | unknown speaker, choose/narrator/close, fullscreen-floating contract | Render, source, `test_app.py` |
| OB-WELCOME | first page and global navigation | Render, source, `test_onboarding.py` |
| OB-CONFIG | recommended, advanced, invalid, macOS, file choosers, backend-dependent controls | Render, source, `test_onboarding.py` |
| OB-DIAG | busy, cancelled, success, actionable/non-actionable error, failed probe | Render, source, `test_onboarding.py` |
| OB-CAL | initial, overlay launch failure, successful return | Render, source, `test_onboarding.py` |
| OB-TEST | initial, busy, cancel-pending, failure, success | Render, source, `test_onboarding.py`, `test_app.py` |
| SETTINGS | all five regions, dependency states, invalid/save, restart markers, 100/150/200% font tests | Render, source, `test_app.py` |
| READY | busy, cancelled, error/warning/ok, failed probe, remediation | Render, source, `test_readiness_ui.py` |
| CAL-OVERLAY | mouse, keyboard suggestion/move/resize/reset/review/cancel, negative monitor geometry | Source, `test_calibration.py` |
| CAL-REVIEW | busy OCR, success, OCR failure, Save/Draw again/Cancel | Render, source, `test_calibration.py` |
| PROFILES | empty, active selected, inactive selected, create/duplicate/rename/remove/use | Render, source, `test_profiles.py` |
| ASSET-MODEL | idle, download, cancel-pending, verify, failure, deferred close | Render, source, `test_asset_ui.py` |
| ASSET-VOICES | empty/changed/valid/invalid manifest, import, busy, deferred close | Render, source, `test_asset_ui.py` |
| VOICE-IMPORT | empty, file selection, missing character/references | Render, source, `test_asset_ui.py` |
| MAC-PERM | granted/not granted/unavailable/failure/request/open-settings/refresh-on-return | Render, source, `test_macos_ui.py` |

## Ranked findings

### P0 - safety and irreversible consequence

1. **Profile removal hides additional data loss.** The confirmation says only
   `Remove 'profile'?`, but acceptance deletes both the stored profile and all
   profile-scoped OCR corrections (`profiles_ui.py:152-166`). There is no undo,
   export, or explicit default-cancel contract. **Verified.** Required decision:
   replace the generic Yes/No prompt with `Remove profile and its OCR corrections`
   plus an exact consequence/count, with Cancel as the safe default. Consider an
   undo only if deletion cannot cheaply be made recoverable.

### P1 - broken or misleading primary journeys

2. **Calibration review has two controls with the same effective behavior.**
   `Draw again` and `Cancel` both reject the review; the overlay treats every
   rejection as “show the overlay and reset the selection”
   (`calibration.py:133-135, 248-260`). `Cancel calibration` therefore does not
   cancel calibration. **Verified.** Keep `Draw again`; make Cancel close the
   calibration flow, or rename it `Back to selection` and provide an actual
   cancel path.

3. **The tray bypasses the Settings auto-advance safety policy.** Settings
   disables and clears auto advance for screen capture and disables it for
   sequence-manual mode, while the tray `Auto advance dialogue` action remains
   enabled and directly persists any checked value (`app.py:1156-1158`,
   `durable_settings.py:9-20`). The controller then reports `Auto advance enabled`
   even when screen capture makes focus verification impossible; manual sequence
   is the only suppressed status (`controller_components.py:899-920`).
   **Verified.** One shared policy must drive both controls and their explanation.

4. **First run silently opts into automatic key dispatch while hiding its
   permission and behavior.** New settings default to auto advance enabled and
   guarded sequence-auto mode (`settings.py:82,125`). The recommended onboarding
   view asks only for a game window; auto advance is not disclosed, and the macOS
   permission control is under `Show advanced options` (`onboarding_ui.py:231-314`).
   Diagnostics discovers the missing permission only on the next page.
   **Verified behavior; heuristic priority.** Put a plain-language auto-advance
   choice beside game-window capture and surface required permissions before
   diagnostics. Safe default should be decided explicitly, not inferred from the
   capture source.

5. **Closing the unknown-speaker prompt grants a session-wide narrator fallback.**
   `Continue with narrator` is explicitly offered, but Escape/window-close executes
   the same fallback (`app.py:2357-2378`). The fallback remains for that character
   in the current controller session (`controller_components.py:1134-1144`). The
   similar live preflight correctly treats close as cancel. **Source-verified.**
   Make close cancel/pause, and label the affirmative action `Use narrator for
   <speaker> this session`.

6. **The asset manager is not backend-aware.** With the default Pocket backend
   and no model ID, `Speech model` selects XTTS, offers `Download / Retry`, and
   saves that XTTS ID without changing the backend (`asset_ui.py:144-183,
   503-530`). Attempting the primary action then raises a CPML-license warning.
   **Verified by default-state render and source.** Hide the model-download tab
   for packaged/no-download backends, or bind its model choices and explanation
   to the selected speech backend.

7. **Settings accepts invalid pack structure and closes before reporting it.**
   Inline validation checks only that Game pack is an existing file. Actual pack
   parsing happens after Settings accepts and closes (`app.py:829-860`,
   `configuration_apply.py:117-139`). Onboarding already validates the same pack
   inline. **Verified.** Reuse the onboarding preflight in Settings before Save.
   Apply the same exact-authority validation principle to voice manifest,
   story index, and sequence plan combinations.

8. **The tray is an unscannable flat command surface.** Runtime introspection
   found 34 rows including separators: 31 named actions, 2 conditional sequence
   actions, and 1 transient cancel-apply action. Setup, diagnostics, assets,
   profiles, OCR tools, voices, history, folder access, and playback are peers.
   Both disabled status and recognized dialogue are also rows. Arbitrary status
   and dialogue text are inserted without truncation (`app.py:1144-1232,
   2554-2575`), so a long OCR line can determine menu width. **Verified count and
   source; heuristic organization.** Keep only Open controls, primary live action,
   essential transport, current short status, and Quit at the first level; group
   Playback, Setup, and Support, and truncate non-action text.

9. **Playback controls claim availability when no operation exists.** Dashboard,
   compact, and tray enable Pause, Skip, Replay, Clear queue, and Emergency stop
   from controller readiness alone (`dashboard_ui.py:404-430, 701-725`,
   `app.py:2578-2617`). They do not track live/speaking/queue/history state.
   **Source-verified.** Drive enabled state from actual capability and put the
   reason beside disabled controls. Emergency stop may remain globally available
   only when there is something it can stop.

10. **Compact mode omits Replay from the required in-game journey.** Full and tray
    expose replay; compact provides Start/Stop live, Read, Pause, Skip, Stop,
    optional expected-line, and `Full` (`dashboard_ui.py:564-625`). **Verified.**
    Add Replay, or explicitly remove replay from the compact mission. Rename the
    vague `Full` to `Full controls`/`Open control window`.

11. **Readiness truncates the component that the user must fix.** At the default
    820 x 500 render, `Character voices` displayed as `Character ...` because the
    Component column is neither sized to content nor stretched; only Details is
    stretched (`readiness_ui.py:47-57`). **Verified visual.** Ensure status and
    component columns fit their content before Details consumes the remainder.

### P2 - information architecture, wording, and accessibility

12. **Runtime setup has three competing concepts.** Dashboard `Setup and
    diagnostics...` opens Readiness; tray `Run setup...` opens the five-page
    onboarding wizard; Readiness can then open Settings, Permissions, Calibration,
    or Voice mappings. **Verified.** Rename the dashboard action to `Check
    readiness...` and reserve `Run setup...` for first-run/reconfiguration, or make
    one setup hub own both flows.

13. **The default macOS Settings viewport is dominated by seven unusable hotkey
    fields.** The first section is Keyboard shortcuts; all recorders are disabled
    on macOS, while a notice repeats that controls must be used instead. At 760 x
    800, this consumes roughly the first third of the viewport. **Verified visual.**
    Hide unsupported recorder rows on macOS and keep one notice, or start at the
    first actionable section.

14. **Settings exposes storage topology as ordinary decisions.** Game pack,
    Voice manifest, Story index, Live sequence plan, Live speaker corpus, and
    Generated audio manifest appear beside ordinary voice settings, with editable
    raw paths and separate Browse controls. The sequence-mode labels include
    rollout/authority terminology. **Verified inventory; heuristic disposition.**
    Keep Game pack as the ordinary import/replace operation; move independent raw
    authority files and rollout mode into a clearly technical section. Derive them
    from a verified pack whenever possible.

15. **Language, model, speaker, and profile fields expose internal identifiers.**
    OCR/TTS languages are free-form codes, model is a raw editable ID, Narrator
    speaker is free text, and Voice profile shows lower-case implementation values.
    **Verified.** Prefer discovered/supported choices with human labels and retain
    manual entry only behind an advanced escape hatch.

16. **Several fields have a value but no trustworthy programmatic label.** Qt
    accessibility inspection returned the selected value as the computed name for
    multiple non-explicit combo boxes. In Asset Manager the editable Model combo's
    computed name was the XTTS model ID because its visual `Model` label is not a
    buddy. Settings/Onboarding tests explicitly cover only composite fields, not
    all selectors. **Verified probe.** Add explicit accessible names/descriptions
    to semantic selectors and status/progress elements; do not rely on selected
    value fallback.

17. **Profile activation understates its consequence.** The summary shows window,
    OCR language, and voice manifest, but applying a profile can also replace game
    pack, story index, sequence plan/mode, generated-audio manifest, audio policy,
    assignments, and calibration (`profiles_ui.py:200-220`; profile schema tests).
    **Verified.** Show the meaningful deltas or a concise content/audio/capture
    summary before `Use selected profile`.

18. **Profile creation labels do not describe cloning current state.** `New...`
    creates a profile from current settings and calibration. Empty-state prose is
    the only disclosure, and validation errors close the name prompt before showing
    a warning (`profiles_ui.py:95-108, 227-229`). **Verified.** Rename to `Save
    current setup as profile...` and validate the name without losing it.

19. **Granted macOS permissions still show active Request and Open Settings
    actions.** Status changes only the label; action enablement never follows the
    result (`macos_ui.py:112-122`). **Verified visual/source.** For Granted rows,
    replace both actions with a quiet `Granted` state; keep `Open Settings` only if
    revocation is an intended task.

20. **Voice reference selection does not constrain long file lists.** The
    `References` label concatenates every chosen filename and is not wrapped,
    elided, or scrollable (`asset_ui.py:45-76`). **Source-verified.** Show count and
    a bounded preview/details disclosure.

21. **The dashboard hides the current speaker in the ordinary view.** The Current
    dialogue card shows only text; speaker is inside Technical details
    (`dashboard_ui.py:86-105, 238-259`). **Verified visual.** Move speaker into the
    current-dialogue card. Keep Voice/audio source/confidence/latency/configuration
    technical.

22. **Terminology drifts across the same concepts.** UI strings alternate between
    `dialog` and `dialogue`, `control window`, `Full`, and `dashboard`, plus `voice
    pack`, `voice manifest`, `voice mappings`, and `character voices`.
    **Verified inventory.** Choose one user vocabulary; keep manifest/sequence
    terms only for technical views.

23. **Tray-icon activation has no explicit open-window handler.** No connection to
    `QSystemTrayIcon.activated` exists. The context menu can open either control
    mode, but click/double-click behavior is platform-default only.
    **Source-verified.** Add the native expected activation gesture if live Windows
    and Linux checks confirm the dead end.

## Explicit control accounting and disposition

The names below account for every application-owned interactive control in this
scope. Standard internal combo-box line edits, popup list views, scrollbars, table
headers, and spin-box arrows are treated as parts of their owning control rather
than separate user decisions.

### System tray and transient runtime controls

| Surface | Every named control | First-pass disposition |
| --- | --- | --- |
| Tray: open/status | `Open control window`; `Compact floating controls`; disabled `Starting...`/runtime status; disabled `No dialog detected`/current dialogue | Keep one primary open action; keep compact as a remembered mode rather than equal entry if feasible; keep short non-action status, truncate dialogue or move it to the dashboard. |
| Tray: reading | `Read current dialog`; `Start/Stop live reading`; conditional `Set story position / resync...`; conditional `Use/Choose expected next line`; checked `Auto advance dialogue`; `Pause/Resume speech`; `Skip current speech`; `Repeat last speech`; `Clear speech queue`; `Emergency stop` | Keep, but group under Reading/Playback and apply actual capability state. Rename `dialog` to `dialogue`; move auto advance behind the shared guarded policy. |
| Tray: configuration/support | `Calibrate dialog region`; `Live diagnostics...`; `Settings...`; `Game profiles...`; `OCR corrections...`; `Review uncertain OCR...`; `Prepare offline audio...`; `Run setup...`; `Manage models and voices...`; transient `Cancel settings apply`; `Choose narrator voice...`; `Manage character voices...`/dynamic `Manage voice for <speaker>...`; `Dialogue history...`; `Diagnostics and logs...`; macOS-only `macOS permissions...`; `Open settings folder`; `Quit` | Keep capabilities, not the flat placement. Put setup/configuration behind one hub/submenu; merge or clearly separate Live diagnostics vs Diagnostics and logs; put folder under Support; keep transient cancel beside active status; keep Quit isolated last. |
| Story-position chooser | Non-editable event selector; `OK`; `Cancel` for `Set story position / resync` | Keep as contextual recovery. Replace the paragraph-sized field label with concise instruction plus separate detail; verify long candidate layout. |
| Expected-event chooser | Non-editable bounded candidate selector; `OK`; `Cancel` | Keep only for multiple candidates; the one-candidate automatic selection is correct. Use the same labels as the dashboard/compact action. |
| Live preflight | `Assign voices...`; `Use narrator for all`; `Cancel live reading`; window-close/Escape | Keep all three outcomes. Rename narrator scope to `Use narrator for these speakers this session`; keep close equivalent to Cancel. Place safe Cancel as default/Escape and verify native order. |
| Unknown speaker | `Choose voice...`; `Continue with narrator`; window-close/Escape | Keep explicit choices; rename narrator scope and make close/Escape cancel rather than silently choose it. |

### Dashboard and compact controller

| Surface | Every named control/readout | First-pass disposition |
| --- | --- | --- |
| Dashboard header/card | runtime status; `Compact controls`; Current dialogue text | Keep. Add speaker to the dialogue card. Compact is correctly top-right but should say `Use compact controls` if it changes the remembered launch mode. |
| Dashboard availability | Reading-control availability/recovery reason | Keep when blocked or transitioning; shorten/hide the always-ready sentence if status and enabled state already communicate it. |
| Dashboard Reading | `Start/Stop live reading`; `Read current dialogue` | Keep position and primary hierarchy. |
| Dashboard Offline audio | explanation; `Prepare offline audio...` | Keep as a secondary mission; cross-check ownership with Daily-tools review. |
| Dashboard Playback | `Pause/Resume`; `Skip`; `Replay`; `Emergency stop` | Keep grouping; gate by real state. Emergency stop remains visually separated. |
| Dashboard Technical details | `Show/Hide technical details`; Mode; Speaker; Voice; Audio source; OCR confidence; Latest latency; Configuration | Keep disclosure. Move Speaker out; merge Mode with runtime status if redundant; keep Voice/source/confidence/latency; replace raw Configuration block with concise active profile/capture/audio summary plus a Settings link only if needed. |
| Dashboard Sequence details | Cursor state; Story position; Event/line; Canonical dialogue; Expected audio; Actual audio; OCR activity; guidance; `Use/Choose/No expected next line`; `Set story position / resync` | Keep conditional and technical. Keep recovery action emphasized only when required; disable expected action with adjacent reason. |
| Dashboard Setup/support | `Setup and diagnostics...`; `More/Fewer setup options`; `Calibrate capture`; `Narrator voice`; `Support and logs`; `Settings`; `Quit VNTTS` | Rename primary to `Check readiness...`; keep secondary disclosure; keep Calibrate/Voice/Support/Settings; Quit is duplicated with window close/tray but justified for background-mode discoverability. |
| Compact information | Mode; runtime status/warning; Speaker; availability/recovery reason | Keep, but suppress redundant ready prose when all controls are available. |
| Compact actions | `Start/Stop live`; `Read`; `Pause/Resume`; `Skip`; `Stop`; conditional `Use/Choose expected next line`; `Full` | Keep and capability-gate; rename Stop to `Emergency stop` if that is its actual consequence; rename Full to `Full controls`; add Replay because the scoped mission requires it. |

### Onboarding wizard

| Surface | Every named control | First-pass disposition |
| --- | --- | --- |
| Global wizard | `Cancel`; `Back`; `Next`; final-only `Finish setup` | Keep native decision order and busy guards. Finish label and handoff are clear. |
| Welcome | No page-local interactive control | Keep the page only if it materially sets expectations; otherwise its text can become the wizard header and save a step. **Heuristic/YAGNI candidate.** |
| Configuration: recommended | Capture source selector; editable Game window; `Refresh...`; Game pack path; game-pack `Browse...`; `Show/Hide advanced options`; validation summary | Keep capture/window/refresh. Replace raw pack path with `Import game pack...` while preserving its automation benefit. Keep disclosure and errors; show success less prominently. |
| Configuration: shortcuts | Read once hotkey; Live reading hotkey; macOS-controls notice | Keep on supported systems; hide the two disabled fields on macOS and keep one notice. |
| Configuration: engine/voice | Speech engine; TTS model; OCR language; TTS language; Narrator reference plus `Browse...`; Voice manifest plus `Browse...`; Narrator speaker; XTTS CPML acceptance plus license link; Pocket authenticated-cloning opt-in plus terms link; `Download model or import voices...`; macOS `Check permissions...` | Keep engine and required legal consent; use human/discovered choices. Move raw model/language/narrator-speaker/manifest controls to technical setup. Keep narrator/voice action only when backend readiness needs it. Move Permissions into recommended flow when required. |
| Diagnostics page | selectable result list; contextual `Fix selected issue`/`Open setup options`/`Manage models and voices`/`Open macOS permissions`/`Show installation help`; `Run checks again`; `Cancel checks` | Keep behavior. Reuse shared diagnostic presentation/remediation logic with Readiness while retaining onboarding navigation semantics. |
| Calibration page | `Calibrate...` | Keep. Rename `Select dialogue area...` if calibration is unfamiliar; preserve instructions and completion gate. |
| OCR-to-speech test | `Run OCR-to-speech test`/`Run test again`; `Cancel test`/`Cancelling...`; progress | Keep; during idle, hide disabled Cancel rather than giving it equal full-width weight. Preserve truthful cancellation and finish gate. |

### Settings

| Region | Every named control | First-pass disposition |
| --- | --- | --- |
| Chrome | Section selector; validation summary; `Save`; `Cancel` | Keep. Remember/select the first actionable section; `Save changes` is clearer. Keep failures prominent and success quiet. |
| Keyboard shortcuts | Read once; Live reading; Pause or resume; Skip speech; Repeat speech; Clear queue; Emergency stop hotkey recorders; macOS-controls notice | Keep where supported; hide unsupported recorders on macOS. Keep collision validation. |
| Capture and OCR | Screenshot directory plus `Browse...`; Capture source; editable Game window plus `Refresh...`; Minimum OCR confidence; OCR language; Save uncertain frames checkbox; Diagnostics directory plus `Browse...` | Keep capture/window. Keep screenshot/diagnostic storage for privacy/control but consider an Advanced subsection. Replace OCR language code with installed choices; keep manual escape. Confidence belongs in Advanced. Dependent diagnostic directory placement is correct. |
| Speech and voices: ordinary | Speech engine; Audio source policy; Narrator reference plus `Browse...`; Voice profile; XTTS license checkbox/link; Pocket authenticated-cloning checkbox/link | Keep with human labels and backend-dependent visibility. Narrator reference should route through voice management where possible. Title-case profile choices and explain their effect. |
| Speech and voices: technical files | Speech model; TTS language; Game pack plus `Browse...`; Voice manifest plus `Browse...`; Story index plus `Browse...`; Live sequence plan plus `Browse...`; Live speaker corpus plus `Browse...`; Generated audio manifest plus `Browse...`; Narrator speaker | Keep Game pack as verified import. Move the rest to Technical/Advanced or derive from the pack; replace raw IDs/codes with constrained choices where feasible. Do not expose independent files when the pack is authoritative. |
| Playback and automation | Output volume; Speaking speed; Auto advance; Sequence-first rollout; Speaker announcements; Advance key; Advance delay | Keep volume/speed/announcement. Keep auto advance with explicit risk/scope. Move sequence rollout jargon to Technical. Key/delay stay conditional and secondary. |
| Application behavior | Warm up model and voices; Launch automatically when I sign in; Keep reading in background when control window closes | Keep. Hide Launch at login off macOS instead of leaving a disabled platform-specific row. Keep close behavior because it changes the window-close consequence. |
| Path choosers | two directory choosers and seven file choosers, each with editable field plus `Browse...` | Native chooser is appropriate. The larger problem is asking for nine paths; reduce ordinary-surface count through pack/asset ownership rather than custom picker work. |

### Readiness, calibration, profiles, assets, and permissions

| Surface | Every named control | First-pass disposition |
| --- | --- | --- |
| Readiness | result table; contextual `Fix selected issue`/`Open Settings`/`Open Permissions`/`Open Calibration`/`Open Voice mappings`; `Run checks again`; `Cancel checks`; `Close` | Keep all; fix column sizing and reuse diagnostic/remediation component logic. Close during checks already cancels safely. |
| Calibration overlay | mouse drag; Enter/Space suggested region and review; arrow move; Shift+arrow resize; Ctrl+arrow coarse step; `R` clear; Escape cancel | Keep direct mouse/keyboard model. Add a visible/announced message for too-small selections; verify fixed 64-pixel instruction panel at 150/200% text. |
| Calibration review | read-only OCR result; `Save region`/`Save region without OCR preview`; `Draw again`; `Cancel`; Ctrl+Return; Ctrl+R | Keep explicit save fallback and Draw again. Repair Cancel semantics. Verify platform-native shortcut notation. |
| Profiles | profile selector; `New...`; `Duplicate...`; `Rename...`; `Remove selected profile`; `Use selected profile`/`Already active`; `Cancel` | Keep capabilities. Rename New to disclose cloning current setup; enrich activation summary; repair destructive confirmation and name validation. Disabled active/removal explanations are good. |
| Profile name dialogs | text field; `OK`; `Cancel` for New, Duplicate, Rename | Keep native input, but validate blank/duplicate inline without discarding typed text. |
| Profile removal/error dialogs | generic `Yes`; `No`; standard warning `OK` for create/duplicate/rename/remove/use failures | Replace destructive Yes/No wording as P0. Warnings are justified but should return focus/value to the initiating control. |
| Asset tabs/chrome | Speech model tab; Character voices tab; `Save`; `Cancel` | Keep tabs only when both missions apply to the active backend. Save/Cancel are correct; report restart/runtime-apply consequence before close. |
| Asset model | editable Model selector; model-directory readout; model status; progress; `Download / Retry`; `Cancel download`; `Verify checksums` | Make backend-aware. Keep status/progress/cancel/verify. Change Download label by state instead of combining Download/Retry; constrain arbitrary model entry or mark it Advanced. |
| Asset voices | Active voice manifest field; `Browse...`; `Validate`; manifest status; progress; `Import voice pack...`; `Add character voice...` | Keep. Prefer Import/Replace as ordinary ownership and make raw path editing advanced/read-only if not required. Existing exact-digest validation and stale-result handling are strong. |
| Voice import | Character; Aliases (comma-separated); References summary; `Choose audio files...`; `Save`; `Cancel` | Keep Character/References/chooser/save/cancel. Keep Aliases because speaker matching needs them, but explain examples or make optional. Bound the filename summary and use inline validation. |
| macOS permissions | Screen recording status, `Request`, `Open Settings`; Accessibility status, `Request`, `Open Settings`; `Refresh status`; `Close` | Keep statuses, Refresh, Close. Show Request only when requestable/not granted; show Open Settings only when it is the next remediation. Keep row-local failure/status behavior. |

## Native-dialog call-site accounting

| Owner | Call sites and controls | Judgment |
| --- | --- | --- |
| Settings | Screenshot directory and OCR diagnostics directory folder choosers; narrator reference, game pack, voice manifest, story index, live sequence plan, live speaker corpus, generated-audio manifest file choosers | Native chooser types/filters are appropriate. Consolidate technical-file decisions rather than replacing native dialogs. |
| Onboarding | optional game-pack, narrator-reference, voice-manifest file choosers; window-list failure warning; calibration-launch warning | Choosers are correctly scoped but too many are exposed under Advanced. Inline failure is preferable where the initiating row remains visible; calibration cannot continue, so warning is acceptable. |
| Runtime | story-position/resync item chooser; multiple expected-event chooser; live preflight; unknown-speaker prompt | Keep contextual and fullscreen-floating. Repair narrator scope/close semantics; test long candidates and 200% text. |
| Profiles | New/Duplicate/Rename text prompts; remove confirmation; mutation/use warnings | Native prompts are sufficient only after inline name preservation and safe destructive wording are added. |
| Assets | multiple-reference audio chooser; active-manifest chooser; import-pack chooser; missing-character/references, missing-model/license warnings; download-cancellation information | File dialogs are appropriate. Move simple missing-input errors inline. Keep license block and close-deferred explanation, but avoid modal repetition when status text can own recovery. |
| Permissions | System Settings URL launch | Correct native destination. Keep local open/failure result and refresh-on-return. |

The support-bundle save chooser is launched from the tray-owned application but
belongs to the Daily-tools Support surface and should be reconciled there.

## Verified strengths to preserve

- Controller/settings/profile lifecycle work is stale-safe and avoids blocking the
  Qt thread; focused responsiveness/cancellation tests pass.
- Dashboard has one visually dominant live action, separates ordinary playback
  from emergency stop, collapses technical details, scrolls at short height, and
  keeps a nearby recovery reason.
- Compact controls stay topmost, avoid capture, preserve status/warnings during
  live mode, and fit dynamically in current 100/150/200% font tests.
- Onboarding blocks forward progress on invalid configuration, incomplete checks,
  calibration, and failed end-to-end test; busy cancellation is truthful.
- Readiness discards stale/cancelled probe results and maps typed remediation to a
  single contextual action rather than parsing prose.
- Settings reports all known validation errors inline and focuses the first error;
  path selectors use native dialogs and labelled composite rows.
- Asset manifest validation is asynchronous and bound to exact path plus digest;
  import/download close is safely deferred.
- Calibration uses a frozen screenshot, normalized geometry, a background OCR
  preview, explicit OCR-failure fallback, and a complete keyboard selection path.
- macOS permission failures and System Settings launch outcomes stay row-local and
  status refreshes on return.

## Missing-capability evidence

Only capabilities backed by an observed dead end are proposed:

1. **Compact Replay**: ordinary in-game replay is available only by leaving compact
   mode, using the tray, or remembering a global hotkey that is unavailable on
   macOS.
2. **Safe profile-removal disclosure/recovery**: current UI can irreversibly remove
   profile settings, calibration, and OCR corrections without naming the latter.
3. **Engine-aware asset action**: default Pocket users are sent to an XTTS action
   that cannot succeed without unrelated license acceptance.
4. **Inline pack authority validation**: Settings closes before proving the
   selected game pack is usable, so a failure forces the user to reopen it.
5. **State-specific transport availability**: users can activate Pause/Replay/Clear
   when nothing exists to pause/replay/clear and receive no local explanation.
6. **Explicit tray activation**: source provides no click/double-click route to the
   control window; validate on real Windows/Linux before adding it.
7. **Current speaker in ordinary dashboard view**: the app detects and routes by
   speaker, but the user must open Technical details to see who the current line is
   attributed to.

No new custom picker, setup framework, profile-icon system, search box, or general
undo framework is justified by current evidence.

## Test and visual gaps

1. No real Windows run and no Cocoa-native verification of tray menu, tray
   activation, native dialog ordering/default buttons, VoiceOver, fullscreen Space,
   or multi-monitor placement.
2. Tray tests assert action presence/delegation, not scanability, long status/OCR
   width, conditional grouping, keyboard traversal, or activation gestures.
3. Prompt tests do not cover unknown-speaker window-close/Escape semantics, long or
   markup-like speaker names, 200% text, multiple screens, or screen-reader output.
4. Dashboard/compact scaled-font tests check containment but not maximum physical
   screen width, logical focus order, state-specific transport capability, or
   speaker visibility.
5. Onboarding scaled-font coverage targets Configuration only; Welcome,
   Diagnostics, Calibration, and Test have no 150/200% visual/focus traversal.
6. Readiness compact test checks containment but not header sizing; the default
   render already clips `Character voices`.
7. Settings has no full keyboard journey across section navigation, dependent
   controls, and Save; accessibility coverage is concentrated on composite path
   rows, leaving several combos dependent on Qt fallback naming.
8. Asset tests cover manifest keyboard order but not model selector labelling,
   default-backend relevance, all-tab focus order, many reference filenames,
   scaled fonts, or restart consequence.
9. Profiles have no keyboard/focus, long-name, scaled-layout, inline name-error,
   destructive default-button, or OCR-correction deletion disclosure test.
10. Calibration has geometry and keyboard tests but no high-contrast/bright-game
    visual, 150/200% instruction text, too-small-selection feedback, or distinct
    Draw again vs Cancel integration test.
11. Permissions behavior is tested with fakes, but granted-state action reduction,
    scaled layout, and real System Settings round-trip are not.
12. No automated screen-reader assertions verify live status/progress announcements;
    current tests mostly check static accessible names/descriptions.

## Cross-surface concerns for central reconciliation

1. **Setup ownership:** reconcile onboarding, Readiness, dashboard `Setup and
   diagnostics`, tray `Run setup`, Settings, Assets, Permissions, Calibration, and
   voice mapping into one understandable hierarchy. Do not independently polish
   all duplicate entry points.
2. **Diagnostics component:** Onboarding Diagnostics and Readiness implement almost
   the same busy/result/remediation/retry/cancel flow with different widgets and
   labels. Share policy/data presentation if it reduces divergence; do not force a
   framework merely to remove a small amount of code.
3. **Configuration authority:** decide whether Game pack or independent manifest/
   index/sequence paths are the ordinary source of truth. The current UI presents
   both simultaneously and lets users form inconsistent combinations.
4. **Voice vocabulary and scope:** align narrator voice, character voices, voice
   mappings, voice pack, voice manifest, session fallback, and permanent assignment.
   Every narrator action must state whether it affects one line, one speaker for the
   session, or saved configuration.
5. **Runtime control parity:** tray, dashboard, compact, and hotkeys should use one
   capability state and one verb set. Current surfaces differ on Replay, Clear,
   sequence recovery, auto-advance safety, and Stop/Emergency stop wording.
6. **Status ownership:** arbitrary runtime status is mirrored into tray, dashboard,
   and compact. Define short state, user action, and technical detail separately so
   errors do not become huge tray rows or repeated prose.
7. **Restart/apply semantics:** Settings marks selected fields as restart-required,
   Assets reports restart after acceptance, while profile selection restarts the
   controller immediately. Central synthesis should make “saved”, “applied now”,
   and “restart required” use the same language and placement.
8. **Destructive behavior:** profile removal is the only destructive setup action
   found here. Its exact correction/calibration consequence and recovery policy must
   match OCR-tool ownership in the Daily-tools audit.
9. **Platform split:** macOS has no hotkeys and therefore depends heavily on compact
   controls, yet Settings starts with disabled hotkeys and compact omits Replay.
   Treat these as one macOS journey issue, not two isolated wording fixes.

## Recommended central synthesis order

1. Fix the profile-removal consequence and calibration Cancel semantics.
2. Establish one shared runtime capability model for tray/dashboard/compact,
   including auto-advance policy and Replay parity.
3. Choose setup/configuration ownership: first-run vs readiness vs settings vs
   assets, and Game pack vs raw authority paths.
4. Normalize voice scope, dialogue terminology, and restart/apply language.
5. Apply accessibility/column/long-text fixes, then run real macOS and Windows
   visual/assistive-technology checks.
