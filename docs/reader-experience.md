# Reader experience

Desktop reading design. A player opens a scene in a visual novel, turns on
narration, and returns to the game. Success is hearing the right line, keeping
control of the pace, and knowing how to stop. The window is a companion to the
game. It needs to occupy the player's attention only while they have a decision
to make.

## Everyday navigation

**Reading** opens on an ordinary launch. **Stories** opens the library for a
deliberate chapter choice, preparation or resuming a saved preparation job.
**Voices** opens the existing editor. Settings and support have one stable
secondary menu. Explicit entry points such as opening a prepared story or
editing a voice still open the requested destination. A running job can show
a quiet link back to its progress without taking over Reading.

## One Reading window

Keep three things together in a fixed order: the game window picker, the
Start/Stop button and a short status. A returning player can change the game or
start with the saved selection from the same row. On first launch the picker
has a “Choose a game window” placeholder. Opening it lists the running windows.
Reopening it refreshes the list. The picker and action keep their order and
remain reachable at a narrow window width.

```text
Reading

Game window
[ Reverse: 1999                 v ]   [ Start narration ]
Ready to read.

> Reading options
```

Starting, waiting for focus, speaking, pausing and stopping update the same
status line. For example, “Return to the game” and “Speaking: Vertin” explain
what is happening. The action changes in place to Cancel start or Stop
narration. Keep its position and keyboard focus. A running session stays
stoppable even when paused. Selecting another window ends the old session and
its queued speech before binding the new target. The new game waits for Start.
Opening and dismissing the picker leaves the current session alone.

Keep **Reading options** below the controls. Its ordinary setting is whether
the game should advance after speech. Reflect the saved choice and key binding.
Changing a default for new installations is a separate product decision.
Show the speaker and selected line on Reading as soon as dialogue arrives.
Keep the line visible while paused or waiting, and clear it when the game changes.
Raw recognition and audio routing are available in Details.
Voice editing, chapter preparation and technical diagnostics retain their own
entry points. The launch screen does not need copies of their controls.

## Set up only what the selected game needs

- Preselect a uniquely matched saved game window. A missing or ambiguous game
  leaves the picker available. If there are no windows, the status asks the
  player to open the game on a dialogue scene. Opening the app never starts
  speech or sends a game key.
- Reuse verified capture areas and voice choices. If a required area is
  missing, offer **Set up and start** beside the picker. Carry the chosen
  window into setup. Mark the dialogue box and choose **Read this line** to
  preview it. The wider Story area is optional for Reading and can be added
  later in Settings for captions elsewhere in the scene. A preview never
  advances the game.
- Show one relevant repair at a time. For example, offer the system capture
  permission when capture is blocked. On a frame timeout, offer **Retry capture**
  for the selected window. Request macOS Accessibility permission
  for automatic advancement when needed. Manual advancement requires no
  synthetic keypress.
- Preserve the initiating action. Completing required setup for Start can
  continue that start once. Editing options by choice only saves options.
  Cancel, failure or changing the target invalidates the pending start and
  stale previews. Automatic advancement requires verified game focus.

## Prepared chapters and in-game controls

- The ordinary Start action follows the visible scene. The existing playback
  policy chooses original speech, matching recordings and permitted live
  fallback. A previous chapter selection must not silently restrict a new
  screen-reading session. Preserve an explicitly configured manual mode.
- In Stories, **Read this chapter** carries the selected chapter and its playback
  rules through setup, loading and return to the game. Show the chapter title
  beside the selected game in Reading so the player knows which session they started. If the required
  audio is still being prepared, explain the wait and link to the job.
  Starting another workflow must not discard completed recordings or replace
  a playing session without the player's intent. If activation fails, keep the
  previous playable selection and offer retry. When matching a chapter fails,
  offer a new match, position selection or live reading only as supported by
  the current mode.
- **Compact controls** is a reversible choice. It is an optional shortcut opened by the player, never an automatic stage of startup or playback. Keep Pause/Resume, Stop and a way back to the full window. Reuse the same session
  across full view, compact view, tray and supported shortcuts. Showing the
  strip over a game leaves focus in the game. Expanding controls is explicit
  and keeps the session running. Keep Stop reachable without global hotkeys.
- Keep **Read current line once** as a secondary action when available. Keep
  manual indexed reading directly reachable when it is how the player advances.
  Put engine/model, detailed voice routing, OCR and readiness reports under
  **Details** or Settings. Show recovery controls at the point of failure, with
  a stable way to reach manual story position in manual mode. Remove the empty
  transcript before the first line, duplicate status prose and the permanent
  bottom toolbar of diagnostic actions from the everyday screen.

## Acceptance scenarios

- Give a new reader an open scene. They should find the game window, confirm
  a line and hear the next one without visiting general Settings. A window
  already chosen on Reading carries into setup. Cancelling setup never starts
  playback.
- Ask a returning player to start, hear a line, change windows and stop. Opening
  the picker leaves the session alone. Committing a different window ends the
  previous session and leaves the new one ready for an explicit start.
- Ask the player to locate page turning and then read a specific prepared
  chapter. Verify that the selected chapter and saved choices survive setup.
- Check focus loss, withdrawal of a capture permission, a closed game window,
  and a late callback after cancellation. A stopped session must never resume
  itself, and automatic continuation must not advance another application.
- On native desktop, exercise keyboard-only access, enlarged text and a screen
  reader. The selected game, current state and Stop must remain identifiable.
