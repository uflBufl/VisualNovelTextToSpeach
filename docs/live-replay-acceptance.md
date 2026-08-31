# Device-free live replay acceptance

## Capture a real session without synthesizing audio

`vntts-capture-live-replay OUTPUT_DIRECTORY` uses the current calibrated
capture mode, dialog region, OCR language/confidence and correction dictionary,
but does not initialize TTS or open an audio device. It stores every distinct
cropped frame as lossless PNG with SHA-256. Duplicate fingerprints are counted;
empty and low-confidence OCR observations remain checksum-bound evidence in
`observation-ledger.json` instead of disappearing before review.
Press Ctrl+C to finish, or bound an unattended diagnostic capture with
`--duration-seconds` or `--max-accepted-frames`. The output directory must not
already exist.

Pass `--story-index STORY.jsonl` to bind unique exact/normalized-exact lines to
their canonical line ID, text and declared source-audio metadata. The index is
hashed before capture and rechecked before publication. A changed or symlinked
index, output, frame or result blocks publication. `capture-report.json` is
written first and `corpus.json` last as the completion marker; interrupted or
failed sessions deliberately retain their partial frame directory for diagnosis
but are not replayable until a valid corpus exists.

With a story index, capture no longer interprets typewriter prefixes, temporary
nameplates or fading text as dialogue boundaries. Every distinct frame is first
written to the immutable observation ledger. A separate dialogue view promotes
only an exact canonical story line or a standalone ellipsis observation;
unmatched text stays explicitly unresolved. Repeated observations of the same
canonical line are grouped by line ID. Once the first canonical line identifies
a chapter, later exact matches are restricted to that chapter, so short OCR such
as `He` or `who` cannot jump to an unrelated chapter. A visually isolated
three-dot glyph is recognized even when OCR reads only its nameplate or animated
background. For that special case, the nameplate may restore a speaker only from
the checksum-bound story index's configured speaker names. Without a story index,
the compatibility path still groups same-speaker prefix-related text and records
inferred boundaries for review. Saved frame specifications contain path and SHA
only: replay reruns OCR on the real pixels and never treats capture-time text as
a declared observation fixture.

Example:

```bash
uv run vntts-capture-live-replay \
  "$HOME/vntts-evidence/rhiannon-raw" \
  --name "Rhiannon Character Story real capture" \
  --story-index /path/to/story-index.jsonl

uv run vntts-recover-live-replay-capture \
  "$HOME/vntts-evidence/rhiannon-raw/corpus.json" \
  "$HOME/vntts-evidence/rhiannon-recovered" \
  --story-index /path/to/story-index.jsonl \
  --sequence-plan /path/to/live-sequence.json \
  --complete-visible-chapter

uv run vntts-seal-live-replay \
  "$HOME/vntts-evidence/rhiannon-recovered/corpus.json" \
  "$HOME/vntts-evidence/rhiannon-sealed" \
  --story-index /path/to/story-index.jsonl \
  --sequence-plan /path/to/live-sequence.json \
  --no-generated-audio-manifest \
  --mode audio-manual

uv run vntts-replay-live \
  "$HOME/vntts-evidence/rhiannon-sealed/corpus.json"
```

The capture command makes the real-game gate reproducible; it does not itself
prove OCR quality, correct dialogue boundaries, source/generated route choice,
audio-device behavior or complete visible-chapter coverage.

Recovery is non-destructive and fail-closed. It validates the raw corpus,
capture report, observation ledger, frames, story index and sequence plan by
checksum. It publishes a new replayable raw corpus only when observations form
one explicit branch-free successor path meeting the requested event count and
silent-event gate. Besides exact canonical observations, OCR drift may resolve
only against speech line IDs declared by that exact plan: one sufficiently long
unique canonical prefix, a bounded OCR suffix, a high-similarity candidate with
a clear margin or, after an exact cursor anchor, weak OCR evidence against the
one and only plan-authorized visible successor. The latter accepts nameplates
captured in the text crop, truncated short lines and bounded OCR drift, but
never chooses among multiple successor candidates. A short candidate is
rejected when it is also the prefix of another authorized line. Intervening
typewriter/background noise may be absorbed only when the next accepted
observation is the current event again or
its one explicit successor; a skipped event, branch, manual boundary,
uncertainty or ambiguity breaks the run. The report records every absorbed
observation. Only the best representative frame is retained for each recovered
event, while duplicate/typewriter frames remain bound in provenance. Arbitrary
unresolved text is never converted to a silent event; only a standalone visual
or textual `...`/`…` may bind the unique explicit silent frontier. An
insufficient run produces `recovery-report.json` only, including the longest
recovered run and the exact shortest branch-free event segment to capture next.
The original directory is never rewritten. Representative recovery frames must
pass the same dialogue-presence gate as production; a bright partial frame is
preferred to a more complete frame already fading out.

`--complete-visible-chapter` derives the gate from the plan instead of an
arbitrary number. Transitions that cannot appear in the dialogue box do not
inflate the target. For chapter `314601`, the contract is therefore 92 visible
events (89 speech and 3 silent), not 100 of the plan's 104 total events.

If the first or last frame of one capture is not production-replayable, recover
an overlapping segment with `--start-event-id` and optionally `--end-event-id`,
seal it independently, and audit the checksum-bound union with:

```bash
uv run vntts-audit-live-replay-coverage \
  "$HOME/vntts-evidence/full-visible-coverage.json" \
  --story-index /path/to/story-index.jsonl \
  --sequence-plan /path/to/live-sequence.json \
  --review "$HOME/vntts-evidence/first-sealed/sequence-review.json" \
  --review "$HOME/vntts-evidence/suffix-sealed/sequence-review.json"
```

The union audit rejects failed seals, authority checksum mismatches, unknown,
duplicated or out-of-order mappings, and non-deterministic visible chapter
paths. It reports technical coverage separately from human listening approval;
the latter is never inferred from a passing replay.

The sealing command is the required bridge from raw capture schema version 1
to sequence replay schema version 2. The raw capture directory is never
modified. The command creates a new contained directory and copies the exact
frames, story index, sequence plan and only generated WAVs referenced by the
captured canonical lines. It rejects changed bytes, symlinks, non-monotonic or
skipped visible events, an unresolved first anchor, and any choice/branch whose
next visible event is not unique. A lost nameplate may be ignored only when the
full normalized text selects the one explicit next speech event. A captured
`...` or `…` may bind a line-less silent event only when that event is the one
explicit next visible event; it is retained in sequence metrics but never sent
to TTS. Every new observation-ledger capture must first pass recovery; direct
sealing cannot collapse repeated silent/canonical observations or silently
discard unresolved frames.

Sealing first runs the production-controller replay as a measurement probe,
writes its exact route, OCR/recovery and attempted/confirmed-key counts into the
corpus, and reruns the sealed corpus. Publication fails unless the second run
reproduces the exact identities, counters, routes and byte authorities.
`sequence-review.json` preserves every capture-to-event mapping, the original
boundary reason and both raw and sealed authorities. Its measured baseline is
software evidence, not a human verdict: review every flagged inferred boundary
or text-only/silent mapping against the playthrough before using the corpus as
acceptance evidence. `replay-report.json` is the passing sealed replay report.
The configured story index, sequence plan, generated manifest and audio-source
policy are used when their command-line options are omitted. Use
`--no-generated-audio-manifest` to override an inherited manifest explicitly;
this is required for an isolated `live-tts-only` acceptance run and prevents a
configured generated fallback decision from silently changing the probe.

`vntts-replay-live` exercises the production split capture/OCR state machine,
incremental dialogue tracker, exact story resolver, generated-audio verifier,
route preparation/playback outcome, completion seal and auto-advance state.
Capture frames advance only after OCR consumes that exact image; a dropped or
replaced capture therefore cannot silently skip a fixture state. A route may
finish early, but a prefix-started route keeps auto-advance in retryable
`visual-wait` until recognition has confirmed the full canonical line. Changed
typewriter frames cannot be interpreted as deterministic successors while that
barrier is active. Final success additionally requires the full corpus frame
ledger to be consumed. Real-pixel replay uses the production
OCR confidence threshold and preprocessing-profile search. A zero threshold is
not used because it would stop at the first non-empty noisy preprocessing pass
instead of exercising production recognition.

The prefix replay uses a generated WAV shorter than the remaining typewriter
animation. Besides one route and no early key, it requires a privacy-safe
`canonical-full-text` event proving that first PCM occurred before the final
glyph. This makes early playback measurable without retaining dialogue text.

### Repaired chapter 314601 capture evidence

The 2026-08-29 repaired Rhiannon capture retained 86 distinct real frames. The
fail-closed recovery selected 21 consecutive visible events, sequences 4-23 and
25, including the separate Hotelier and Rhiannon silent events at sequences 18
and 19. It retained one representative frame per event and absorbed the other
typewriter/background observations in provenance. The production sealer then
passed two consecutive `audio-manual`, `live-tts-only` runs with all 21 frame
identities consumed, all 21 canonical event identities reproduced, 19 speech
routes, two silent routes, zero skipped frames and zero key dispatches. OCR was
invoked for all 21 representative frames and 18 observations used bounded
recovery. This closes the 20-event software replay gate; operator mapping review
of the Hotelier and Rhiannon silent events was accepted on 2026-08-29. The
later full capture recovered all 92 visible chapter events. Its stable 91-event
suffix passed the production sealer twice, and the checksum-bound union with
this accepted 21-event seal proves 92/92 technical coverage. Silent event 78
received explicit human mapping approval on 2026-08-30, so the union report now
also proves complete human acceptance for all three silent events.

An original-game route must declare `source_audio_duration_seconds` when replay
is expected to advance automatically. Without observable completion, production
correctly returns `passthrough-unobserved` and blocks the key rather than
guessing when the game's audio ended. The tracked smoke corpus declares a 1 ms
device-free completion duration for this reason; it passes with both routes,
all four frames and exactly two completion-bound advances.

Corpus frame entries may remain a path string for real OCR replay, or use an
object with `path`, `sha256`, `observed_character` and `observed_text`. The object
form is a deterministic recognition fixture: bytes must match the declared
SHA-256, the production image fingerprint remains part of the key, and the
declared observation replaces OCR only after capture/fingerprint. Reports retain
the corpus digest, `fixture_kind`, every frame digest, and whether each consumed
observation came from OCR or a declared fixture value. An observation is invalid
without its exact frame digest. Serialized media paths are relative, contained
under the corpus directory and may not traverse symlinks. This is useful for
state/routing regression, but is not evidence of OCR quality on those pixels.
Each recognized event is mapped to its one-based dialogue/frame index, relative
path and digest. `frame_consumption` reports declared, consumed and skipped event
counts plus every frame's consumed state; skipped duplicate/stale recognitions
remain visible but cannot substitute for a missing declared frame.

An optional `generated_audio_manifest` object binds a contained relative `path`
and exact `sha256`. Every manifest audio path is independently contained and
bound by its declared WAV SHA-256. The runner copies the one verified manifest
and WAV byte snapshot into a private temporary bundle, then loads and decodes
that snapshot through the production manifest/index/library validators. A
manifest or WAV mutation after corpus loading fails instead of changing the
route. Generated playback uses a device-free output sink that consumes the exact
decoded float32 PCM and reports sample rate, sample count and PCM SHA-256. It
deliberately reports no device underrun claim.

## Sequence-first replay contract

Schema version 2 adds a required `live_sequence` binding while retaining schema
version 1 compatibility. The binding declares exactly `mode`, `story_index`,
`plan`, `expected` and `focus_probes`. `shadow`, `audio-manual` and the explicit
production `audio-auto` mode are accepted. The story index and sequence plan
are contained relative files with
exact SHA-256 values. They are re-read, revalidated and copied into a private
snapshot immediately before execution, so changed bytes, symlinks, mismatched
canonical speaker/text or a plan that no longer binds the declared line IDs
fail the replay instead of changing its authority.

Every version-2 dialogue record binds a canonical sequence `event_id`. A speech
record also binds its exact story-index `line_id`; a line-less silent event uses
`line_id: null`, sets `expect_playback` to false and must not declare a source.
A record that plays audio must declare its exact `expected_source`. The expected
block binds the ordered event IDs and nullable line IDs plus exact counts for distinct
OCR-routed frame identities, bounded recoveries, attempted key dispatches and
confirmed key dispatches. The report also retains raw OCR invocations, which
may be higher when automatic-transition confirmation rechecks the same frame.
This distinction prevents an implementation detail from weakening the bounded
OCR acceptance gate.

Version-2 replay constructs the production `AppController`, `StoryCursor`,
canonical line resolver, route preparation and playback callbacks. Saved-frame
capture and device-free audio remain test adapters. Each consumed frame records
whether it was routed from OCR or from the locked canonical cursor. A cached
confirmation frame cannot consume a second ledger event, so stale or duplicate
work remains visible without advancing the declared corpus.

In `shadow`, production auto-advance requests and focus checks are exercised,
but the replay frame source substitutes for the real key device. In
`audio-manual`, playback completion advances the declared frame source as a
separate simulated player action and the expected key count remains zero. An
`audio-auto` replay uses canonical full-line routing, requires the latest
cursor-owned frame to be visible and stable, rechecks focus before each
device-free key, and confirms the pending key from the next explicit stable
event before another event can advance. A canonical line is queued once without
waiting for the incremental OCR tracker after the cursor has resolved its exact
line ID. An
ambiguous initial observation, including identical repeated canonical lines,
fails closed without speech; it requires explicit position selection or a later
unambiguous recovery rather than an OCR guess.

When the current explicit successor is a line-less silent event, exact ellipsis
OCR can acknowledge it in shadow, audio-manual and audio-auto mode. This special case
does not generalize punctuation into a story line: the successor must be unique,
must be typed `silent` by the plan and contributes a nullable line identity to
the replay report. Audio-manual replay models the player's next click separately;
shadow replay counts the silent event's confirmed dispatch like any other
automatic visible event.

The tracked sequence corpora are:

- `samples/sequence-live-replay-shadow.json`: focus loss, long typewriter
  reveal, internal ellipsis, original/generated/live routes and three confirmed
  automatic dispatch attempts;
- `samples/sequence-live-replay-audio-manual.json`: the same canonical route
  order with player-driven frame changes, no automatic dispatch and two bounded
  recoveries caused by lost nameplates/manual skips;
- `samples/sequence-live-replay-audio-auto.json`: three cursor-owned automatic
  keys across original/generated/live routes, including focus loss and return;
- `samples/sequence-live-replay-audio-auto-safety.json`: a silent dialogue,
  passive transition and explicit choice/manual boundary, with exactly one key
  per eligible automatic event and bounded recovery after the choice.

All four are regression gates. They prove deterministic software behavior, not
real game focus, OS input delivery or audio hardware. Focused synthetic tests
separately cover a pure `...` event with no synthesis, identical ambiguous
lines, branch selection, bounded multi-line skip, visual-wait postponement and
an unconfirmed key that is never retried.

The tracked `samples/rhiannon-live-replay-representative.json` covers:

- Rhiannon's short `I, erhm ...` through a declared original-game route;
- a Hotelier incomplete prefix which must wait for the exact complete line;
- Adar Llwch Gwin Fledgling through one checksum-verified generated entry and
  unique prefix expansion;
- Narrator through live synthesis fallback;
- exact observed dialogue order, route order, four completion-bound advances,
  complete per-frame consumption, no duplicate/stale/skipped dialogue, and
  generated PCM consumption.

The sample reuses saved project images and a checked-in WAV with explicit
fixture observations. It is representative software evidence only. The
following remain separate acceptance gates: a captured real-game Rhiannon corpus
with transient nameplate/background OCR noise, a 20-line gameplay run, voice and
pronunciation listening, reference contamination review, real audio-device
underrun/stop/prefill behavior, Windows capture/hotkeys and a long soak.

Narrator fallback selection and the separate force-live override are
controller/UI routing gates covered by focused tests. They do
not compare Centurion/Paper Heron perceptually. Exact generated-manifest lookup,
manual-override bypass, corrupt/missing/checksum/metadata fallback and reserved
prefix behavior are covered by generated-audio tests and the replay matrix.

## Pregenerated playback latency boundary

A 2026-08-29 trace of the current Character Story pack showed that local WAV
preparation was not the reported post-text delay. Three generated routes reached
first PCM within 0.8-2.2 ms after their route decision, while five repeated OCR
runs on the retained 738x128 dialogue crop took 0.33-0.42 s each. The removable
latency was before routing: after a static dialogue the split capture loop could
observe a changed fingerprint and still sleep once more on the previous 600 ms
idle interval before capturing the stability-confirming frame.

The capture loop now caps the next interval at the fast interval immediately
when the dialogue fingerprint changes. It still requires two stable
observations, so the optimization does not turn one transient OCR frame into
speech. Unfocused mode performs no OCR and probes focus at most 500 ms apart,
instead of allowing the former 1.6 s focus-return delay. Generated-prefix
resolution searches the current chapter first; a global same-speaker scan is
retained only when the current chapter has no matching candidate. On the same
real index this reduced a warm Narrator-prefix lookup from 90-96 ms to
0.14-0.18 ms (0.9 ms on its first verified-WAV lookup).

The sequence-owned path no longer adds the former fixed 1.5-second prefix
dwell. A unique bounded prefix can route immediately only after its exact WAV
passes checksum/metadata preflight; a live fallback is never started from that
prefix. The visual frame must still settle before automatic key delivery. The
unique next generated speech event is preflighted during current playback, so
the later prefix route normally consumes reserved PCM rather than reopening the
WAV. Replay and real-game acceptance still need to prove zero wrong, duplicate,
skipped or early-advanced lines under slow typewriter, truncation, branches,
focus loss and rapid manual skips.
