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

An unexpected OCR speaker outside the established lookahead still fails closed:
speech waits and the existing voice-choice prompt opens. A session without a
story index has no trustworthy speaker corpus to preflight, so it retains this
runtime behavior until an explicit corpus contract is provided.
