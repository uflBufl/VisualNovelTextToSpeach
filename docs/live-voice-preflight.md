# Live voice preflight

Story-index-backed live reading checks upcoming named speakers before capture
starts. The preflight scope is the current inferred chapter beginning at the
current matched line and is capped by the configured chapter lookahead. It does
not scan unrelated chapters or claim whole-story coverage.

If the story index is loaded but the current chapter is not known, live reading
does not start. The prompt asks for one `Read current dialog` pass; after that
line establishes the chapter, starting live reading runs the scoped preflight.

For each line in scope, no decision is needed when one of these routes is
already authoritative:

- the exact source speaker is `???`, which synthesizes as Narrator;
- Narrator is the source speaker;
- a configured or confidently resolved character voice exists;
- the exact line will use verified original game audio.

Every remaining named speaker is shown before live reading. The user can open
voice assignment, explicitly approve Narrator for all listed speakers for this
live session, or cancel. Assignment is followed by a fresh scope check; live
reading starts only after it passes. Narrator approvals expire when that live
session ends, so a later session must make a new decision.

The controller enforces the same preflight for every start, including the
historical command-line hotkey and restarts after modal voice/history windows.
The tray is the richer decision UI, but direct callers cannot bypass the
controller guard. A voice satisfies the guard only through the same exact
registry resolution used by the synthesis backend after high-confidence OCR
canonicalization; a nearby name such as `Selene` does not silently resolve to
`Selone`.

Clicking the bulk Narrator choice does not trust the speakers captured when the
prompt opened. The tray recomputes the current scoped set, requires an exact
match, stages that set, and then the controller recomputes it again immediately
before starting. Any change refreshes the prompt or blocks the start.

An unexpected OCR speaker outside the established lookahead still fails closed:
speech waits and the existing voice-choice prompt opens. A session without a
story index can instead configure `live_speaker_corpus`, a version 1 JSON object
with a non-empty, case-insensitively unique `speakers` list. The entire explicit
list is preflighted before every start. Narrator and exact `???` need no choice;
other named speakers still need an assigned/resolved voice or a fresh one-session
Narrator approval. A configured corpus that is missing, damaged, empty, or has
duplicate/invalid names blocks live start rather than silently reverting to an
empty scope. `VNTTS_LIVE_SPEAKER_CORPUS` provides the equivalent environment
override. See `samples/rhiannon-live-speakers.json` for a representative file.
