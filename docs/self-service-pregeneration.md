# Self-service pregeneration

**Status:** Current product and implementation contract. For the ordinary Stories
procedure, see [Playing](playing.md). Recorded acceptance results at the end of
this page describe historical runs, not the state of a local preparation job.

## Player workflow

Offline audio is prepared by each player for the stories they choose. The
application owns discovery, voice planning, generation, bounded recovery,
validation, publication and activation. Expert workspaces, queue IDs, checksums
and per-line review remain outside the default journey.

`OfflineAudioPreparationDialog` in `vntts/pregeneration_ui.py` has these stages:

1. `Choose stories`: select detected or imported content and stories. `Find game`,
   `Game folder...` and the additional import controls discover supported content.
   `Check story audio` inspects existing coverage.
2. `Choose and confirm voices`: inspect the planned character routes and narrator.
   `Listen or change voice...` opens the voice selector. `Apply voice changes`
   replans changed controls; `Generate with these voices` starts preparation.
3. `Generate and check audio`: show durable progress, permit cancellation and run
   supported automatic recovery. Saved work survives cancellation and restart.
4. `Activate and read`: report original audio, prepared WAVs, live fallback and
   explicit omissions separately. `Use prepared audio` activates the saved pack;
   playback starts only through a reading action.

Automatic activation is allowed only when the caller requests it, no live
fallbacks remain and the activation guards still permit it. Otherwise the saved
pack stays inactive until the player explicitly activates it. Closing the final
screen preserves the current audio setup. A finished preparation with live
fallbacks is playable, but it is not entirely offline.

## Voice decisions

`pregeneration_voices` combines exact source evidence, explicit assignments,
saved voice-library decisions and eligible reference candidates into a
checksum-bound `VoicePlan`. Distinct exact queue bindings remain separate even
when their display names match. Narrator fallback is an explicit route; a similar
voice or portrait is not proof of character identity.

The confirmation screen exposes the planned narrator and character routes before
generation. The player can inspect an existing voice, choose another candidate,
or explicitly link names with `Link same person...` and undo that link with
`Unlink name`. Identity suggestions do not themselves rewrite source names.

`VoiceAuditionPanel` in `pregeneration_audition_ui.py` is a candidate selector,
not the old mandatory A/B questionnaire. It provides original-reference playback,
a generated `Preview`, editable preview text, `Game phrase` to cycle available
source excerpts and `Save` for the selected voice. When comparing a replacement,
it can show the current voice separately. Saved choices bind the selected source
and applicable controls. A failed preview or persistence operation exposes its
error and retry path; it does not invent a completed voice decision.

Reference checksums and safe contained paths are verified before use. Candidate
ranking and optional speaker embeddings help order evidence; they do not grant
synthesis or review authority. See [speaker identity diagnostics](speaker-identity-diagnostics.md)
for the model and held-out threshold boundary, and
[source-reference review](source-reference-review-import.md) for expert import.

Offline engine settings are resolved independently of live preferences. Pocket
uses its `default` profile; Qwen uses `stable`; invalid profiles fall back to the
registered default. Model setup and reference admission are backend-specific.
Allowlisted embedded Pocket voices are reference-free model inputs; cloning
routes require real validated reference audio. There is no general rule that
silently switches every unsupported MOSS configuration to Pocket.

## Immutable inputs and resumable state

`pregeneration_setup` discovers versioned content artifacts, stores the selected
story/line identities and estimates preparation resources. The job identity
binds the imported source revision and selected scope. Incompatible or damaged
saved work cannot silently become a valid resume target.

Voice planning and `pregeneration_queue` materialize an identity-addressed
private input directory. It contains the selected story records, effective voice
manifest, validated reference copies, exact queue and `input.json` hash ledger.
Selected semantic source-audio evidence is projected to the chosen stories and
validated against those records, rather than copied as an unrelated full-import
ledger. Mixed event/speech lines use only explicitly authorized spoken-text
projections; pure unsupported events require an explicit omission route.

Changing a selected reference, voice route, text or synthesis control changes its
input identity. Reusing an identical input does not rewrite immutable artifacts.
Compatible synthesis PCM can be reused through the content-addressed shared cache;
a cache hit supplies audio, not a prior line decision. The working generation
authority is `generation-state.sqlite3`; no adjacent generated-audio manifest is
written during generation or review. See [bulk generation](authoring-bulk-generation.md).

## Generation, quality and recovery

`pregeneration_generation.OfflineGenerationWorker` owns each exact generation
run and its cancellation. It validates the private input and selected controls,
uses durable SQLite progress, and retains an isolated MOSS backend only within
its validated serial generation/recovery session when configured. Other workers
use their owned subprocess boundary. Cancellation waits for the owned worker;
it does not terminate unrelated processes.

Typed completion, file integrity, PCM format, clipping and measured speech-silence
gates determine whether a WAV may become generated audio. Pocket is unseeded
and receives one provider attempt per supported action; an integer attempt
identity is not an applied Pocket sampling seed. Diagnostic pace, repetition or
perceptual-artifact signals must not be described as calibrated automatic
rejection authority without the corresponding held-out evaluation.

`pregeneration_recovery` recomputes an exact failure plan from current queue and
state authority after each batch. It runs only supported bounded actions such as
safe interrupted-work resume, sentence segmentation, edge-silence repair and
eligible seeded retry. A queue/action pair is not repeatedly spent in one
recovery pass. Successful unrelated lines are left intact. Terminal failures may
receive a checksum-bound live-Pocket route carrying the source failure and
selected destination evidence; cancellation remains resumable work instead of
being converted to a terminal fallback. This policy is not a promise of an
automatic offline-Pocket rerender after every non-Pocket failure.

The normal workflow accepts generated audio through its explicit automatic
technical-gate path. It does not require human listening to every clean line.
Expert review remains available and distinguishes automatic acceptance from a
human listening decision. Raw failures, pending human decisions and missing
route authority cannot be shipped as completed WAVs.

## Publication and activation

`pregeneration_pack.OfflinePackPublisher` requires terminal coverage and publishes
an immutable portable pack containing selected story records, effective voices,
approved WAVs, explicit live fallbacks, omissions and projected source evidence.
A compatible earlier self-service pack can form a cumulative successor: unchanged
unselected content is retained, while the current selection replaces its earlier
routes. Changed source text and incompatible game/version or pack provenance
block unsafe reuse.

Publication uses staging, checksum validation and atomic no-replace rename. The
public importer and runtime generated-audio loader reopen the staged artifacts.
An identical validated publication is reusable; a changed state produces another
identity. Generation state, review diagnostics and partial files are not pack
payload.

`pregeneration_activation.OfflinePackActivator` preflights the published pack,
projects its runtime settings and applies them before committing a separate
`PreparedPlaybackStore` record. When a runtime was already ready, it is stopped
and restarted against the new projection. Activation failure attempts to restore
the previous runtime; rollback failure is reported explicitly. Playback is not
started merely because a pack was saved or activated.

The preparation UI guards activation against intervening settings changes. A
pack that cannot be activated automatically remains saved and can be explicitly
activated later. This separates durable audio publication from the user's
current reading configuration.

## Acceptance boundaries

A complete implementation must prove resume/cancellation without duplicate work,
checksum-bound voice changes, explicit terminal routing of every selected line,
portable no-replace publication and failure-atomic activation. Fast synthetic and
offscreen tests establish workflow behavior; they do not replace real-backend
quality or physical playback acceptance. Historical timings and voice judgments
below apply only to their recorded corpus, model and controls.

### Historical full-scale acceptance (2026-09-01)

The 2026-09-01 full-scale preflight rebuilt Chapter 13 plus `The Eaglet Takes
Wing` from the installed game and selected 3,395 dialogue records: 2,190 exact
original tracks and 1,205 synthesis candidates. After eight ambiguous groups
were resolved through the ordinary automatic-choice path, the private input
reopened with all 1,205 queue items runnable and 15 explicit narrator-fallback
roles. Distinct selected voices for the same displayed character receive
checksum-derived, exact-line internal manifest routes; the queue keeps the
original `speaker`, so this routing identity is not exposed as dialogue UI.

The bounded real-backend canary then generated one character line and one
missing-role narrator fallback with Pocket, automatically accepted both WAVs,
published a two-line pack, reopened it through the public importer and activated
it against isolated settings. Pocket's allowlisted embedded narrator now passes
the same reference-free contract in CLI setup and shared fallback preflight. An
intentional harness abort followed by two exact-job resumes performed no recovery
or acceptance work; both WAV mtimes, sizes and SHA-256 values remained unchanged.

The subsequent full-scale Pocket run completed the same immutable selection as
input `edffcbb31afeefae1c692ca449075bf39a73ca1a41d456a8387afa8d7cb6ef4b`.
It published all 3,395 selected story lines with 1,191 generated and automatically
approved WAVs, three exact live fallbacks and 11 pure-event omissions. The one
mixed speech/event line was rendered separately from its canonical spoken
projection; `*cough*` was not read aloud. The three fallbacks preserved complete
Pocket results rejected for 1.28, 1.36 and 1.44-second internal silences; two used
voice X and one routed Lorentz Butterfly through Marguerite. The published pack
identity is `d0ec668c0ff4efdf11a5960aece662d30cb6003955332d4e1c9817c02015404e`;
its generated-audio manifest contains exactly 1,191 WAV entries, three fallback
entries and 11 omission entries, and the public importer reopened all 3,395 story
records before isolated activation. Re-materializing under the audio-event-aware
input schema reused the shared synthesis cache while repeating the quality gates.

Reopening that terminal job now validates its state and WAVs once, starts no
speech backend, performs zero recovery actions and zero acceptance writes, and
reuses the same pack identity. `OfflineGenerationResult` carries its pending
review count through the pipeline, and an already-published identity is reopened
from the immutable pack instead of rescanning generation output at every phase.
The exact full diagnostic harness fell from 70.4 to 39.6 seconds; that number also
includes rebuilding the 152,453-record source selection and voice plan plus
multiple deliberate public-import checks. Within the remaining mandatory WAV
scan, format/peak and speech-silence validation now share one PCM read while
retaining the file SHA-256 and both stored-quality comparisons.
