# Cross-application UI integration review

## Verified inventory boundary

- UI exists in `VisualNovelTextToSpeach` and `reverse1999-extractor`.
- `vntts-artifacts` contains no desktop or web UI.
- The workspace exposes 11 application launch routes. The main application owns a tray
  menu, full and compact controls, guided setup, configuration, preparation,
  correction and support dialogs. Specialist review tools remain separately
  launchable.
- The shipped Windows Inno Setup installer/uninstaller is a separate
  distribution flow. It includes an optional startup task and post-install
  launch action.
- The tray also owns three transient notification states: background-mode
  information, unmapped-speaker warning and critical error.
- Example/demo windows and CLI-only tools are not shipped UI surfaces.

## Shared vocabulary contract

Use these terms consistently in final recommendations:

- `dialogue`: game speech text; reserve `dialog` for an application window;
- `voice`: a narrator/character assignment;
- `reference audio`: source audio used to condition or judge a voice;
- `speech engine`: player-facing backend choice;
- `backend`, `model`, `profile` and `generation controls`: technical authoring
  evidence;
- `prepare offline audio`: ordinary player workflow;
- `generate`: authoring workflow;
- `setup`: the guided first-run/reconfiguration journey;
- `readiness check`: verifies whether play can start and offers direct fixes;
- `live diagnostics`: current capture/OCR/routing evidence;
- `support and logs`: troubleshooting and export after setup.

## Verified cross-surface inconsistencies

1. The existing `docs/ui-ux-audit.md` says it covers every shipped Qt surface,
   but its surface matrix predates offline-audio preparation/voice audition,
   missing-voice reuse review and the two extractor UIs. Its completeness claim
   is stale and must be replaced by the reconciled inventory.
2. Tray text uses `No dialog detected` and `Read current dialog`; dashboard and
   documentation use `dialogue`. The tray wording should use `dialogue`.
3. Compact/full navigation uses three labels for the same view switch:
   `Compact floating controls`, `Compact controls` and `Full`. The target should
   be explicit: `Open compact controls` and `Open full controls`.
4. The tray constructs about thirty actions and acts as a complete application
   sitemap, while the dashboard already owns the ordinary visual hierarchy.
   This duplication requires one cross-surface decision rather than independent
   label polishing.
5. `Setup and diagnostics...`, `Run setup...`, the `Ready to play` readiness
   window, `Live diagnostics...` and `Diagnostics and logs...` use overlapping
   words for distinct jobs. The vocabulary contract above should separate them.
6. Settings exposes raw game-pack, voice-manifest, story-index, sequence-plan,
   speaker-corpus and generated-manifest paths in the same always-visible scroll
   flow as volume and startup behavior. Onboarding already hides technical
   choices under `Advanced options`; Settings does not use the same boundary.
7. Compact `Stop` invokes the same emergency-stop operation labelled
   `Emergency stop` in the dashboard and tray. The compact label understates its
   scope even though its accessible description is accurate.
8. The original Qt-only inventory omitted the Windows installer/uninstaller and
   treated tray balloons as implementation details. Both are user-visible,
   product-owned touchpoints and require platform validation.

## Cross-surface decisions pending agent evidence

- Reduce the tray to common reading controls and a small number of navigation
  actions, or group rare tools in native submenus.
- Reuse onboarding's ordinary/advanced boundary in Settings; keep common capture,
  playback and application behavior visible and move raw artifact/runtime fields
  behind one advanced disclosure.
- Decide whether readiness is the single setup/recovery front door and whether
  the full onboarding wizard should be labelled `Run setup again` after first
  completion.
- Normalize playback verbs (`Play`, `Replay`, `Read`) only where their underlying
  scopes match; do not erase meaningful review distinctions.
- Verify that shared authoring decision context remains readable when long values
  wrap at compact size and 200% text scale.
