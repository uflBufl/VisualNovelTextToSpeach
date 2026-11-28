# Player guide

## Installation and permissions

If you have a packaged build, keep the complete bundle intact. On Windows,
extract the whole archive, then launch its executable from the extracted folder.
For the current checkout, see [Source build](development.md#source-build).
Package maintainers can check the [release requirements](release-speech-runtime.md).

| Platform | Capture and permissions |
| --- | --- |
| Windows 10 version 2004 (build 19041) or later | Run in an interactive desktop session. Use a visible game window in windowed or borderless mode. Minimized and exclusive-fullscreen windows may be unavailable. |
| macOS 14 or later | In **Privacy & Security**, grant **Screen & System Audio Recording** to the app or terminal that launches it, then restart. Grant **Accessibility** to use auto advance. Use Reading or Compact buttons during play. Global hotkeys are unavailable. |
| Linux X11 | Run in an interactive X11 desktop with `DISPLAY` set. Capture is best effort. Native Wayland and headless sessions are unsupported. |

## Read as you play

1. Open a dialogue scene and select the window under **Capture source** on
   **Reading**. Opening the list refreshes the available windows. **Screen
   region** is available when you want to capture a fixed area without auto
   advance.
2. Choose **Start reading**. If prompted, choose **Set up and start**. In
   **Reading areas**, mark the dialogue and speaker regions, then mark a wider
   **Story area** that covers the centre of the scene and the whole dialogue
   box. You can mark it before a central caption appears.
3. Select **Read this line** to hear a preview, then **Save and start reading**.
   Use **Check readiness** to troubleshoot capture, OCR or audio output.

Use **Read once** while Reading is stopped to speak the visible line. During a
session, **Compact** keeps playback controls and the current or last line in a
small floating panel. **Open Reading** returns to the main controls. Pause
interrupts the current line and may replay it from the beginning when resumed.
Stop clears the reading session and its queued speech. Changing the capture
source stops the session, so start reading again after switching windows.

For auto advance, select a game window and keep it focused. VNTTS sends the
configured key after speech completes and checks that the dialogue changes. If
it asks you to advance manually, turn the page in the game. Disable auto
advance or pause before an in-game choice when using ordinary OCR reading.
After the game UI moves or changes size, check the saved reading areas and
recalibrate if text is clipped or missed.

## Prepare a story

1. Open **Stories**. Choose detected content or use **Find game** or **Game
   folder...** to locate an installed Reverse: 1999 copy. **Extracted
   content...** accepts an existing local extraction.
2. Select the stories you want and the engine under **Generate with**.
3. Resolve any voice choices and review the narrator and character routes. The
   summary shows reusable recordings, new work and known failures. After
   changing voices or engine, review the updated summary.
4. Select **Generate with these voices**. The estimated time covers generation
   and may change as lines finish. Final checks and repairs can take additional
   time. Cancel keeps finished work. Reopen the selection and choose **Continue**
   or **Continue preparation** to resume.
5. Choose **Use prepared audio** when offered, or **Read** to continue from a
   ready story. Open that story in the game and start Reading if it is stopped.

The current-dialogue card identifies the audio used for the line. Saved audio
keeps its recorded voice. **Narrator voice** selects the live fallback voice.
To revoice saved audio, prepare a new result with the desired voice choices.
Review any remaining live fallbacks before activating prepared audio.

## Choose where speech comes from

During live Reading, VNTTS lets a recognized and verified full original line
play in the game, regardless of the selected **Audio source policy**. It uses
verified timing to advance. For other lines, **Live TTS only** uses synthesis,
while **Automatic** and **Prefer original game audio** can use
matching prepared recordings before synthesizing. The original-audio policy
also waits for a verified partial game cue before reading the full line.
A manual character voice or the Narrator force-live option can override
prepared audio.

A game pack can supply the story index, voices and optional prepared audio. Set
it in the game profile or in Settings. See [Game packs](game-packs.md) for pack
validation and [Speech engines](speech-engines.md) for voice setup.
