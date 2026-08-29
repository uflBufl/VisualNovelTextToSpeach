# Sequence-first live reading

## Implemented foundation

The first two runtime-safe slices landed on 2026-08-29. They deliberately do
not control the game yet:

- released `vntts-artifacts` v0.7.1 owns the schema-version-1 sequence reader
  and writer, exact story-index SHA-256 binding, source-extract provenance and
  producer identity. VNTTS retains only its runtime cursor;
- validation rejects unknown or duplicated story line bindings, speech lines
  from another chapter, dangling successors, unreachable events, invalid
  automatic/choice/terminal control, cross-chapter edges and unguarded
  automatic cycles. Runtime-transparent passive transitions are part of cycle
  detection, so they cannot hide an automatic loop behind a non-user-controlled
  event;
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
- cursor reads and transitions are serialized by one controller-owned re-entrant
  lock across OCR, playback and UI threads. Stable-frame candidates carry both
  their cursor owner and a frame-route epoch; an explicit frame bind invalidates
  in-flight routing before it can overwrite a newer user selection. Invisible
  or unfocused frames are rejected before even the initial OCR bootstrap path.
- a changed fingerprint during manual advancement is not evidence that exactly
  one dialogue box was crossed. OCR-free routing is therefore limited to a graph
  window with exactly one visible candidate. When two or more visible events are
  reachable, the bounded canonical recognizer identifies the actual box before
  the cursor moves, preventing a rapid two-line skip from speaking the skipped
  intermediate line.
- the control window and tray expose `Set story position / resync` only in
  `audio-manual`. The chooser lists visible speech/silent events with chapter,
  sequence, speaker, text and event ID. A stopped-session choice establishes the
  next start anchor and story scope. During live reading it clears stale queued
  speech, binds the latest frame and routes the selected canonical speech event
  immediately; silent events update position without synthesizing text. Invalid
  or non-visible event IDs fail closed.
- explicit expected-event selection and resync route their canonical line ID in
  the immutable speech chunk. Repeated identical speaker/text pairs cannot be
  re-resolved to the wrong occurrence by mutable chapter proximity. Voice
  deferral leaves the cursor unchanged; a later queueing failure moves it to a
  published fail-closed state and records the terminal outcome instead of
  leaving an invisible partial mutation.
- the same control window now keeps a persistent sequence-first card rather than
  relying on transient status text. It shows cursor state/reason,
  chapter/sequence, event and line IDs, canonical speaker/text and the count of
  explicit next candidates. Plan load, session reset, OCR anchor, playback
  start/outcome, visual transition and resync all publish immutable snapshots.
  Failed playback, desynchronization and branch/manual boundaries make recovery
  guidance durable and emphasize the story-position control; off and shadow
  modes do not present it as a manual action.
- live replay schema version 2 binds the exact story index and sequence plan by
  contained path and SHA-256, revalidates and snapshots both immediately before
  execution, and drives the production `AppController`, cursor, route and
  playback callbacks. Reports bind ordered canonical event/line IDs, distinct
  OCR-routed frames, raw OCR invocations, bounded recoveries, key attempts and
  confirmed keys. Every routed frame states whether OCR or the locked canonical
  path supplied it.
- the tracked shadow and audio-manual sequence corpora pass deterministically.
  Together they cover typewriter prefixes, a lost nameplate, internal ellipsis,
  original/generated/missing-WAV routes, focus loss, manual multi-line recovery
  and duplicate/stale ledger protection. Focused tests additionally prove that a
  pure `...` event advances without synthesis, identical ambiguous initial lines
  produce no speech and branch/choice recovery selects only an explicit bounded
  candidate.
- the real-game recorder writes every distinct crop to a checksum-bound
  observation ledger before deriving dialogue groups. With a story authority,
  typewriter prefixes, transient nameplates and corrupted OCR remain unresolved
  evidence; only exact canonical identities and standalone ellipses are
  promoted. Exact capture binding locks to the first canonical chapter. A
  visual three-dot detector recovers `...` even when OCR sees only the
  nameplate/background, and the nameplate speaker may be restored only from the
  bound story's speaker names. This prevents animation states from becoming
  false boundaries and prevents short text from escaping to another chapter;
- `vntts-recover-live-replay-capture` validates and copies a branch-free exact
  run into a separate raw corpus without changing the source capture. Bounded
  prefix, OCR-suffix and high-margin similarity recovery is limited to the
  checksum-bound plan line IDs. Intervening noise is absorbed only across the
  current event or its unique explicit successor, and only one best
  representative frame is retained per event. It never crosses a branch,
  skipped event or numeric gap, and it emits an exact next-capture segment
  instead of hiding unresolved evidence when the gate is insufficient;
- raw real-game capture remains immutable schema version 1 evidence. The
  `vntts-seal-live-replay` bridge publishes a separate schema-version-2 bundle
  only after copying and hashing its exact story/plan authorities, mapping every
  captured screen to the unique next explicit event, snapshotting only referenced
  generated WAVs, measuring the production controller and reproducing the sealed
  counters in a second run. Its review report never records a human acceptance
  automatically.
- schema-version-2 dialogue identity is event-first. Speech binds both event ID
  and story line ID; a visible line-less silent event binds the event ID with a
  null line ID and cannot declare playback. Exact ellipsis OCR may acknowledge
  such an event only when it is the unique explicit successor. This closes the
  real chapter's previously unrepresentable sequences 18, 19 and 78 without
  pronouncing punctuation or inferring numeric gaps.

`vntts-artifacts` v0.7.0 introduced game-pack schema version 2 with an optional
checksum-bound `live_sequence_plan` core component. Its loader deliberately
retains schema-v1 compatibility, while schema v1 still rejects the new component
because its v0.6.x readers cannot understand it. The pinned v0.7.1 safety patch
also rejects automatic cycles hidden behind passive transitions. Reverse: 1999 source-pack
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

The first full raw replay capture provided 262 distinct frames but the former
typewriter grouping emitted 190 dialogue groups and 186 inferred boundaries.
Only 28 groups mapped to canonical lines; 162 were animation/nameplate noise.
Strict checksum-bound recovery treats the 162 unresolved legacy groups as
barriers rather than assuming they are harmless animation; its longest explicit
run is therefore one speech event, with no silent event. It correctly did not
publish a sealable 20-event corpus. The recovery report identifies sequences
4-23 as the shortest explicit 20-visible-event follow-up, including silent
sequences 18 and 19. This is evidence for the grouping repair, not a passed
real-game acceptance gate. The repaired follow-up capture retained 86 distinct
frames and recovered 21 consecutive visible events, sequences 4-23 and 25,
including the separate Hotelier and Rhiannon silent events at sequences 18 and
19. Its production `audio-manual`, `live-tts-only` seal passed twice with 21/21
frame and event identities, 19 speech routes, two silent routes, zero skipped
frames and zero key dispatches. This closes the 20-event software replay gate;
the Hotelier and Rhiannon silent mappings received human approval on 2026-08-29.
The later full-chapter capture proved that an arbitrary 100-visible-event gate
was impossible: the plan has 104 total events but only 92 dialogue-box events
(89 speech and three silent). Recovery now derives a complete-visible-chapter
gate from the plan. The immutable full capture recovered all 92 events after
bounded fixes for nameplate-contaminated text, curly apostrophes, truncated
short lines and faded representative frames. Its first frame was already
fading and therefore correctly failed the production presence gate; the prior
accepted seal supplies event 4, while a new overlapping suffix seal passed
twice for all 91 events from 5 through 101. The checksum-bound union report at
`rhiannon-visible-chapter-coverage-2026-08-30-v3.json` proves 92/92 technical
coverage with no missing event. The unique silent frontier at event 78 received
explicit human mapping approval on 2026-08-30; together with the earlier
approval for events 18 and 19, the report now proves complete human acceptance.

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
move without full OCR only when the bounded graph window contains exactly one
visible candidate. A longer linear window still requires bounded recognition:
manual input can cross multiple boxes between captures. The implemented gate
requires plausible bright glyphs in the lower dialogue band, rejects empty and
mostly bright popup-like crops, waits for two equal changed fingerprints for at
least 120 ms, rechecks game focus in the consuming worker, and binds the
candidate to the cursor event and frame-route epoch that first observed it.
Visibility loss, focus loss, a changed fingerprint, a changed owner or an
explicit frame bind resets or invalidates the candidate. The three-pixel lower
bound intentionally preserves a small anti-aliased `...` as a visible dialogue
screen.

When there are multiple successors, repeated identical lines, an early manual
advance, or an unexpected fingerprint, use a small expected-candidate recognizer
first. It compares only the allowed successor speakers and discriminating text
anchors. Full OCR is a last recovery stage, never the default loop.

The bounded recovery recognizer is implemented for settled branch, manual and
desynchronized frames. It searches only the current speech event and visible
events reachable through declared graph edges, capped at three visible events
and 24 total nodes. Exact speaker/text is preferred; a corrupted or missing
nameplate may be ignored only when canonical text selects exactly one allowed
line. The cursor can then recover directly to that explicit event. Ambiguous
repeated text and observations outside the bounded set remain silent and leave
the cursor closed. Candidate misses record event IDs and a match-result enum,
never OCR or canonical dialogue text.

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

The persistent dashboard card implements these fields for manual-audio mode.
Its expected-audio value is a non-mutating policy/manifest prediction: status
rendering never prepares audio and never hashes a WAV. The actual-audio value
comes from the typed route trace and is shown only when that trace's line ID is
the current cursor line; a previous line's trace is hidden. OCR activity
distinguishes initial full-OCR anchoring, recovery availability and the normal
locked state where full OCR is idle.

Manual bounded recovery is available from both the dashboard and tray. When
the cursor has one allowed visible lookahead event, `Use expected next line`
selects it directly; this is the explicit escape hatch for consecutive
identical dialogue boxes whose fingerprint cannot change. Multiple allowed
events open a compact chooser containing only the current three-visible-event,
24-node graph window. The controller recomputes the candidates when the action
is applied, so a stale UI event ID cannot move the cursor. Selection clears
stale speech, binds the current frame and routes canonical speech (or preserves
a silent event). Full chapter/line resync remains a separate final recovery
tool.

Compact in-game controls expose the same bounded expected-event action whenever
manual mode has at least one current candidate. It recomputes controller options
when pressed and follows controller lifecycle readiness; full chapter resync
remains in the full dashboard. Game profiles preserve `audio-manual` across
serialization, restart and profile switching.

Timeline diagnostics should record event ID, line ID, previous/next cursor,
route, fingerprint transition, readiness gates, branch candidates, recovery
stage and confidence. Text remains represented by stable IDs/hashes in routine
logs.

The implemented `stable-frame-gate` timeline event records only a shortened
dialogue fingerprint, visibility, focus, cursor owner event ID, candidate-frame
count, settled milliseconds and the final readiness boolean. Cursor observation
and visual-transition events include previous/current event IDs and the cursor
reason. They do not record dialogue text.

## Migration and acceptance gates

Implement behind a `sequence-first` feature flag and keep current OCR-driven
mode available during evaluation.

The automated `shadow` and `audio-manual` phases and their deterministic replay
gate are implemented. Shadow records predictions and exercises the production
dispatch gate through a device-free key adapter. Audio-manual uses full OCR for
its initial anchor, then the visibility/focus/ownership/render-settled gate can
route deterministic successors without another OCR call while leaving all
advancement to the player. Explicit manual resynchronization, bounded
branch/skip recovery and the persistent cursor-status card are implemented.
Automatic sequence-owned key delivery remains gated on real-game route
correctness and the complete-visible-chapter acceptance run.

Required automated cases include long typewriter text, a lost nameplate,
punctuation-only silent dialogue, source audio, generated audio, missing-WAV
canonical fallback, consecutive identical lines, a branch/choice, manual single
and multi-line skips, focus loss, stale preparation and desync recovery.

Production acceptance requires:

- zero speech from OCR text while locked;
- zero wrong-line, wrong-speaker, duplicate or app-skipped dialogue in the
  reviewed replay corpus and a complete-visible-chapter real-game run;
- every visited line with an eligible approved WAV routes to that WAV;
- no full OCR during ordinary linear locked transitions, with full-OCR rate and
  recovery reasons reported explicitly;
- one and only one automatic key per cursor event;
- focus loss, choices, ambiguity and unsupported events pause safely;
- generated playback starts within the existing local-WAV latency target after
  the visual transition is detected.

After these gates pass, remove OCR text from the locked-mode speech path and
retire the old incremental tracker as a fallback-only recovery component.
