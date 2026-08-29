# Sequence-first live reading

## Implemented foundation

The first two runtime-safe slices landed on 2026-08-29. They deliberately do
not control the game yet:

- released `vntts-artifacts` v0.7.0 owns the schema-version-1 sequence reader
  and writer, exact story-index SHA-256 binding, source-extract provenance and
  producer identity. VNTTS retains only its runtime cursor;
- validation rejects unknown or duplicated story line bindings, speech lines
  from another chapter, dangling successors, unreachable events, invalid
  automatic/choice/terminal control, cross-chapter edges and unguarded
  automatic cycles;
- invalid input is validated in a temporary candidate before publication, so a
  rejected write does not replace or create the requested output;
- the session-only `StoryCursor` implements the seven planned states and
  explicit anchor, playback, one-key dispatch, transition confirmation and
  fail-closed desynchronization operations;
- `shadow` rollout mode can be selected with a validated plan and story index.
  It observes only exact canonical story-index line IDs, reports cursor state,
  event/line IDs, next-event count and reason through the privacy-safe pipeline
  event callback, and never changes speech routing or sends an advance key;
- an explicit linear chain of automatic silent and passive transition events
  may be crossed by a later exact speech observation in shadow mode. A choice,
  wait, branch or unplanned line still fails closed, and later observations
  cannot implicitly recover a desynchronized cursor;
- a `transition` event uses passive control: the game advances its own timed
  background, effect or title step and VNTTS observes the successor without
  sending a key. This is distinct from an automatic `speech` or `silent` event,
  where VNTTS may eventually send exactly one configured advance key.
- `audio-manual` uses an exact allowed line observation to update the cursor,
  then replaces OCR speaker/text with the checksum-bound story-index record
  before existing original/generated/live routing. Once locked, an unmatched,
  unplanned or desynchronized observation is not spoken. The mode forces whole
  dialogue chunks and suppresses auto advance even if the saved general toggle
  is enabled; the player remains responsible for every transition.
- `audio-manual` binds the first exact anchoring dialogue fingerprint. After its
  typed playback completes successfully, a changed fingerprint must be observed
  identically twice; the first observation suppresses OCR while the frame
  settles, and the second follows only one deterministic graph path to a speech
  or silent event. Speech uses the exact story-index speaker and text,
  including one full canonical live fallback when no reviewed WAV exists. A
  branch, manual boundary, failed playback or still-playing event remains
  closed. Passive non-dialogue events may be crossed, but a silent dialogue box
  is retained as its own cursor event. Successful canonical live fallback is
  sealed against late OCR suffixes just like generated/source WAV playback.
- the control window and tray expose `Set story position / resync` only in
  `audio-manual`. The chooser lists visible speech/silent events with chapter,
  sequence, speaker, text and event ID. A stopped-session choice establishes the
  next start anchor and story scope. During live reading it clears stale queued
  speech, binds the latest frame and routes the selected canonical speech event
  immediately; silent events update position without synthesizing text. Invalid
  or non-visible event IDs fail closed.

`vntts-artifacts` v0.7.0 publishes game-pack schema version 2 with an optional
checksum-bound `live_sequence_plan` core component. Its loader deliberately
retains schema-v1 compatibility, while schema v1 still rejects the new component
because its v0.6.x readers cannot understand it. Reverse: 1999 source-pack
export can include the plan, and VNTTS pack import selects that exact path or
clears a stale plan when the imported pack has none.

The cross-repository acceptance fixture for chapter `314601` contains 104
events (89 speech, 3 silent ellipses, 11 passive transitions and 1 terminal
event). A real source pack with 13 voice references was published by the
extractor and accepted through `vntts-preflight-game-pack`, proving the shared
writer, version-2 manifest and VNTTS consumer against the same files.

The Reverse: 1999 producer now preserves explicit raw choice targets, treats
only a present `sequence + 1` record as a linear successor and exposes multiple
entry anchors for disconnected branch segments. A complete installed-corpus
round trip through this loader accepted 2,075 chapters and 152,989 events:
125,875 bound speech, 3,205 silent, 21,143 transitions, 19 standalone choices
and 2,747 manual waits. This full graph check is the evidence that no numeric gap
or serialized array position is being turned into an inferred key press.

## Problem and evidence

The current live pipeline treats OCR observations as the authority for speaker,
text, dialogue boundaries and line identity. The story index and generated
manifest are consulted only after OCR has produced a stable-enough string. This
puts the least reliable component on the critical path even when the game pack
already knows the chapter, ordered dialogue, canonical speaker, canonical text,
source-audio metadata and approved generated WAV.

The 2026-08-29 Character Story trace demonstrated the failure mode. One indexed
Rhiannon line was emitted as two Pocket chunks, while an approved Hotelier WAV
was bypassed after OCR lost the nameplate and exposed an incomplete prefix as
Narrator. Local generated playback itself reached first PCM within a few
milliseconds once routing finally occurred; recognition and synchronization,
not WAV decoding, dominated the delay and errors.

The existing story index is useful but not yet sufficient to drive the game
blindly. Chapter `314601` contains 89 indexed lines across sequences 4 through
101, with seven missing ranges: 18-19, 24, 32, 51, 61, 78 and 89-90. Inspection
of the exact installed Unity asset on 2026-08-29 showed that the source actually
contains 104 raw steps with declared order. Sequences 18, 19 and 78 are visible `...` lines
that the speakability filter removed; 24, 32, 51, 61, 89 and 90 are timed
background, camera, audio or effect transitions with empty English text; 1-3
and 102-104 are opening/closing transition and title-card steps. This confirms
that numeric gaps must not be interpreted as key presses and that the producer
must classify the raw array before filtering text. The index still contains no
successor edges or choice boundaries. The active generated manifest covers 60
of its 89 indexed lines, while 15 declare installed source audio.

## Target principle

In a validated game-pack session, the runtime sequence plan is authoritative.
OCR is used only to acquire an initial anchor, disambiguate a branch and recover
from proven desynchronization. While the cursor is locked, VNTTS never speaks
OCR text and never chooses a voice from an OCR nameplate. It routes the expected
line ID directly to original game audio, approved generated audio, or live TTS
of the canonical indexed text.

The control flow becomes:

```text
bootstrap anchor -> locked story cursor -> route expected event
                 -> wait for audio and visual readiness
                 -> send at most one key -> confirm visual transition
                 -> advance through an explicit successor edge

unexpected transition -> desynchronized -> bounded OCR recovery
                                      or explicit manual resync
```

## Producer contract

Add a checksum-bound optional `live_sequence_plan` component to a game pack.
Keep it separate from the general story index so corpus search and runtime
control-flow semantics do not become one ambiguous schema.

Each chapter plan must contain:

- stable event IDs and the source chapter/sequence provenance;
- every advanceable dialogue-box event, including speakable lines, `...`, typed
  audio events, intentional omissions and other silent boxes;
- a story-index line ID for every speakable event, binding canonical speaker and
  text hash without duplicating mutable text;
- explicit event kind: `speech`, `silent`, `transition`, `choice`, `wait` or
  another validated
  game-specific kind;
- explicit successor event IDs, entry points and terminal events;
- branch/choice metadata sufficient to know when automatic advancement must
  pause;
- the producer version and hashes of the exact story index and source extract
  from which the plan was derived.

Publication must fail when a successor is missing, a referenced line identity
does not match the story index, an auto-advanceable cycle has no explicit guard,
or an unexplained producer omission could correspond to a visible dialogue box.
VNTTS must never reconstruct missing events from sequence-number gaps.

## Runtime state machine

Introduce a session-owned `StoryCursor` independent of the incremental OCR
tracker. Its externally visible states are:

- `unsynchronized`: no trustworthy chapter/event anchor;
- `anchoring`: one bounded OCR operation is resolving an exact or uniquely safe
  indexed line;
- `locked`: one current plan event and its allowed successors are authoritative;
- `playing`: the current event route is active;
- `waiting-transition`: one advance key was sent and no second key is permitted;
- `desynchronized`: captured evidence conflicts with the expected transition;
- `manual`: a choice or unsupported event requires the player.

Bootstrap should use one exact or unique current-chapter OCR match. A user may
also select a chapter/start event explicitly, avoiding OCR altogether. The
cursor persists only session progress, never mutates the immutable pack.

For a locked speech event, routing receives the event's line ID directly. The
speaker and text come from the checksum-bound story index. If an approved WAV
exists, it is prefetched and played. If it does not, Pocket or another selected
fallback synthesizes the full canonical line once. OCR output cannot replace or
append to that text.

For a locked silent event, VNTTS schedules no TTS but still waits for visual
readiness before it may advance. Choice and unsupported events pause automatic
control and explain the required manual action.

## Visual synchronization without full OCR

The capture loop remains necessary, but its normal locked-mode job is cheaper:

1. detect that the dialogue region is present;
2. detect a changed dialogue-region fingerprint after an advance;
3. detect that typewriter rendering has settled for a bounded interval; and
4. confirm focus before any key dispatch.

Audio completion alone is insufficient because a short WAV can end while text
is still rendering. Visual stability alone is insufficient because animation or
an unchanged repeated line can create false transitions. The advance gate is
therefore `audio complete or silent event`, plus `dialogue render settled`, plus
`game focused`, plus `cursor still owns the same event`.

In a linear successor step, a verified dialogue-region transition is enough to
move to the one explicit successor. Full OCR is not run. When there are multiple
successors, repeated identical lines, an early manual advance, or an unexpected
fingerprint, use a small expected-candidate recognizer first. It compares only
the allowed successor speakers and discriminating text anchors. Full OCR is a
last recovery stage, never the default loop.

## Desynchronization and manual play

Fail closed rather than speaking an OCR guess. A mismatch must stop automatic
key dispatch and preserve the current route evidence. Recovery proceeds in this
order:

1. match the current event or its explicit successors using lightweight anchors;
2. search a small monotonic lookahead window in the same chapter;
3. run full OCR and require an exact or uniquely safe current-chapter match;
4. offer explicit `Use detected line`, `Previous expected`, `Next expected` and
   `Choose chapter/line` recovery actions.

A manual click during playback interrupts or suppresses stale audio. One
observed successor transition advances the cursor once. Multiple rapid manual
advances require lookahead recovery; the system must not assume one key press or
one changed frame equals one skipped line.

## UI and diagnostics

The tray/dashboard should expose one consistent live-session card:

- chapter and `current / total` event position;
- current canonical speaker, abbreviated text and line/event ID;
- expected route and actual route: game, generated or live fallback;
- cursor state, allowed next event count and last synchronization reason;
- whether full OCR is idle, anchoring or recovering;
- a visible desync explanation and explicit recovery actions.

Timeline diagnostics should record event ID, line ID, previous/next cursor,
route, fingerprint transition, readiness gates, branch candidates, recovery
stage and confidence. Text remains represented by stable IDs/hashes in routine
logs.

## Migration and acceptance gates

Implement behind a `sequence-first` feature flag and keep current OCR-driven
mode available during evaluation.

The automated `shadow` and `audio-manual` phases are implemented. Shadow only
records predictions. Audio-manual uses full OCR for its initial anchor, then a
two-frame stable visual transition can route deterministic successors without
another OCR call while leaving all advancement to the player. Explicit manual
resynchronization, branch selection, a stronger dialogue-presence/render gate
and automatic advancement remain gated on deterministic replay and real-game
route correctness.

Required automated cases include long typewriter text, a lost nameplate,
punctuation-only silent dialogue, source audio, generated audio, missing-WAV
canonical fallback, consecutive identical lines, a branch/choice, manual single
and multi-line skips, focus loss, stale preparation and desync recovery.

Production acceptance requires:

- zero speech from OCR text while locked;
- zero wrong-line, wrong-speaker, duplicate or app-skipped dialogue in the
  reviewed replay corpus and a 100-event real-game run;
- every visited line with an eligible approved WAV routes to that WAV;
- no full OCR during ordinary linear locked transitions, with full-OCR rate and
  recovery reasons reported explicitly;
- one and only one automatic key per cursor event;
- focus loss, choices, ambiguity and unsupported events pause safely;
- generated playback starts within the existing local-WAV latency target after
  the visual transition is detected.

After these gates pass, remove OCR text from the locked-mode speech path and
retire the old incremental tracker as a fallback-only recovery component.
