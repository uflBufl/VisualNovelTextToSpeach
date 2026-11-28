# Sequence-first live reading

**Status: current runtime architecture and acceptance limits.** Sequence modes
are implemented, but deterministic replay does not qualify native input or
real audio hardware. Use [Playing](playing.md) for player operation,
[Typed live audio routing](live-audio-routing.md) for audio selection, and
[Replay acceptance](live-replay-acceptance.md) for capture/seal commands.

## Purpose and modes

When a verified story index and event plan exist, OCR identifies the visible
event; the plan-bound story line supplies canonical speaker and text. Recognition
noise cannot replace the spoken payload. A general OCR session remains available
for games without those assets.

| Mode | Routing and control |
| --- | --- |
| `off` | Ordinary OCR-driven reading; no sequence cursor authority |
| `shadow` | Observe canonical matches and report cursor predictions without changing production speech or key dispatch |
| `audio-manual` | Canonical event routing; every game transition remains the player's action |
| `audio-auto` | Canonical routing with one guarded application key per eligible automatic event |

`AppSettings.live_sequence_mode` currently defaults to `off`.
`auto_advance_enabled` is a separate setting and defaults to true; ordinary
window-based OCR reading can therefore use auto advance without a sequence
plan. In `audio-auto`, sequence control remains unavailable without its verified
index and plan. Migrated settings preserve explicit mode choices; older missing
fields and a historical `audio-auto` setting without a plan have dedicated
migration rules in `settings.py`.

## Producer authority

The shared `vntts-artifacts` package owns sequence serialization and validation;
`vntts/live_sequence.py` owns the runtime cursor. A game-pack schema-version-2
manifest may include a checksum-bound `live_sequence_plan`; schema version 1
remains readable and cannot declare that component.

A chapter plan carries stable event IDs, source provenance, entry points,
explicit successors and terminal boundaries. Speech references a story line and
its text hash. Visible ellipses remain typed silent events; passive transitions,
choices and manual waits are represented explicitly. Array position and numeric
gaps never create a key press.

Validation rejects mismatched or duplicated line bindings, dangling successors,
unreachable events, cross-chapter edges and unguarded automatic cycles,
including cycles hidden behind passive transitions. Candidate output is
validated before publication, so a rejected write does not replace its target.

An externally selected matching plan can accompany a pack that omits one.
Reapplying that same pack preserves the external plan and mode; switching to a
different pack clears an unowned plan, and an embedded plan takes precedence.

## Runtime ownership

```text
initial anchor -> locked cursor -> route canonical event
               -> wait for audio and visual readiness
               -> send at most one guarded key
               -> confirm a changed stable frame
               -> follow an explicit successor

unexpected transition -> bounded recovery or explicit resynchronization
```

Initial OCR must identify an exact or uniquely strong plan-bound line.
Low-coverage prefixes and close competing candidates do not establish an
initial anchor. Once locked, every speech path resolves the cursor event to its
line ID and verifies the text SHA-256 against exact UTF-8 text before routing.
Unmatched, unplanned and desynchronized observations cannot escape into generic
live TTS, history updates or unknown-speaker prompts.

A controller-owned re-entrant lock serializes cursor transitions across OCR,
playback and UI threads. Frame-route epochs invalidate in-flight work after an
explicit frame bind. Each anchor creates an event occurrence; playback carries
an immutable `(event_id, occurrence_id)` lease. Late completion from an earlier
visit cannot complete a newly reanchored event. Its terminal ledger accepts one
successful canonical route or intentional silence and rejects duplicate keys.

Focus, visibility, cursor ownership and stable-frame evidence are checked before
routing and again before dispatch. Focus-probe failure is a retryable
`focus-wait`, never permission to send input. A pending key is confirmed from a
subsequent explicit visual transition; it is not blindly retried. Passive timed
events do not receive keys. Choices, waits, manual boundaries and ambiguity
pause automatic control.

A manual advance during playback suppresses stale audio. A changed frame does
not prove that exactly one event was crossed. OCR-free manual routing is limited
to a graph window with one visible candidate; rapid skips and competing
successors require bounded recognition. A visible line-less silent successor
can be confirmed from an exact ellipsis or the restricted sequence-owned visual
gate without pronouncing punctuation.

## Early playback and visual completion

Full OCR serves initial anchoring, bounded ambiguous recovery and explicit
resynchronization. Locked linear progression can use the canonical cursor and
visual evidence instead of re-recognizing every line. The production timeline
distinguishes canonical routing authority from actual OCR evidence.

During current playback, a unique immediate generated speech successor can be
prefetched on the synthesis executor. Verification reads, hashes and decodes one
WAV snapshot. The reservation binds backend authority, owner event, line ID and
text hash; stale results cannot route as another event. Prefetch neither
synthesizes live audio nor crosses branches, silence, choices or waits.

A reserved generated immediate successor can start after an application-owned
single key and a focused, visible, changed stable frame. Bounded unique generated
prefixes can also start after checksum/metadata preflight. These paths retain a
separate canonical-full-text barrier: audio may start before typewriter rendering
finishes, but the next key must wait. Cursor-supplied text cannot satisfy that
barrier as if it were OCR.

Completion can be established by canonical OCR, a continue cue or 800 ms of
owner-bound render quiet on the same focused visible event. Render quiet cannot
select a successor. The capture loop retains fast cadence while this barrier is
open; changed typewriter glyphs reset the quiet timer. Missing/corrupt WAVs,
ambiguous prefixes and live-only prefix routes retain the ordinary exact gate.

Audio policy and semantic source completeness are owned by
[Typed live audio routing](live-audio-routing.md). Full authoritative original
speech remains game-owned in live mode. A partial cue can precede the full
reading; unknown completeness cannot masquerade as a complete source route.

## Manual recovery and diagnostics

Recovery searches the current event and explicit successors, then a small
monotonic same-chapter window. Full OCR must leave one safe canonical candidate;
otherwise the user chooses position. Numeric sequence gaps and a single changed
frame never imply how many lines the player skipped.

Manual-audio mode exposes bounded expected-event recovery and full story-position
resync. A stopped-session selection establishes the next anchor. During reading,
a valid choice clears stale speech, binds the latest frame and routes the
canonical line, or preserves a typed silent event without synthesis. Candidates
are recomputed when applied so a stale UI event ID cannot move the cursor. The
compact controls share the bounded action; full chapter selection remains in
the full window.

The cursor snapshot carries chapter/sequence, event/line IDs, state/reason,
canonical content and candidate count. Expected audio is a non-mutating
policy/manifest prediction; rendering status does not prepare audio or hash
WAVs. Actual audio is displayed only when its typed route trace belongs to the
current line. Technical diagnostics and recovery use those shared snapshots.

Manual diagnostics refresh requests fresh capture/OCR and uses a generation to
reject late results. A screen-capture diagnostic window conceals itself during
capture and must restore on success, failure or timeout. Passive pipeline
snapshots do not substitute for a requested refresh.

Routine timeline records use stable IDs, shortened fingerprints and allowed
metadata instead of dialogue text. Declared stages include stable-frame gates,
cursor transitions, canonical-prefix visual rechecks, canonical-full-text,
successor prefetch and withheld-advance reasons. Observational recording must
not abort routing. Speaker-announcement completion is separate from canonical
dialogue completion and never owns an advance key.

## Acceptance evidence and limits

[Replay acceptance](live-replay-acceptance.md) owns corpus schemas, capture,
recovery, sealing and coverage-union instructions. The tracked shadow,
manual, automatic and automatic-safety corpora exercise the production
controller with device-free capture/input/audio adapters. Assertions bind event
order, speaker, source, per-frame consumption, recovery and attempted/confirmed
keys. Route integrity requires no wrong, duplicate, stale or app-skipped dialogue.

Historical motivation came from a Rhiannon line split into live typewriter
chunks and a generated Hotelier recording bypassed after nameplate loss.
The chapter `314601` plan has 104 total events but only 92 visible speech/silent
boxes; complete-visible acceptance derives its target from the plan. The
August 2026 capture/seal work reported full technical coverage and explicit
human mapping approval for its silent events. These are dated corpus results,
not a present-day production qualification claim; the external artifacts are
not shipped as a reproducible repository gate.

Production qualification still requires a real-game focus/choice run, listening
for correct voice and timing, driver underrun/stop behavior, Windows input and
capture, and long-session behavior. Keep these distinct from a passing software
replay. In particular, require:

- canonical payloads while locked, with no OCR guesses spoken;
- correct line/speaker/order and an approved WAV whenever eligible;
- bounded full-OCR recovery with reasons recorded;
- one key per eligible event, with focus loss and unsupported boundaries safe;
- no early advance while text is still rendering;
- local generated playback latency measured separately from source cues and
  optional speaker announcements.

The incremental tracker remains necessary for ordinary OCR reading; retiring
that supported path is not an outstanding migration step of sequence mode.
