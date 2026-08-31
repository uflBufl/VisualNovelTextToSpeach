# Self-service pregeneration

## Product contract

Pregeneration is a normal player workflow, not a service performed once by a
project author. VNTTS is not expected to ship pregenerated speech for every
chapter. Each user prepares the stories they want locally with minimal choices
and no knowledge of authoring workspaces, artifact manifests or model controls.

The default journey is:

1. Choose `Prepare offline audio` in the application.
2. Select a detected game installation and one or more stories or chapters.
3. Review a small set of ambiguous character voices. Each decision uses a short
   synthesized audition that demonstrates the resulting voice, not merely an
   unexplained source clip.
4. Leave VNTTS to extract, generate, validate, repair and package the selected
   content. The job is cancellable and resumable.
5. Start playing with the automatically activated local pack.

The normal path exposes no workspace paths, JSON manifests, queue IDs, model
IDs, seeds, retry controls, checksums, publication commands or per-line approval
queues. Those remain available only in expert diagnostics.

## Minimal human decisions

VNTTS automatically reuses exact original game speech, known character aliases,
previous voice decisions and technically valid references. Reference candidates
remain separated when portrait, age, bank or speaker evidence conflicts. Only a
genuinely ambiguous group asks the user to listen.

One voice card answers a player-level question: "Which voice should this
character use?" It presents the character and visual context when available,
then a fixed synthesized phrase for the best candidate. The actions are:

- `Use this voice`;
- `Try another`; and
- `Use narrator`.

A checksum-bound decision is reused in later stories while its character
variant, ordered reference audio, backend/model and generation profile remain
unchanged. A changed control invalidates only the affected voice group. Clean
generated lines do not require human approval. Optional expert review may inspect
exceptions, but abandoning that review never blocks creation of a playable pack.

## Automatic pipeline

The self-service orchestrator owns the whole chain:

1. Discover or request one supported game installation.
2. Run the game-specific importer behind a versioned content-artifact boundary.
3. Enumerate selectable stories and classify each line as original audio,
   synthesis candidate, live fallback or intentional non-speech omission.
4. Resolve reusable voice decisions and collect only unresolved auditions.
5. Select a supported local backend and stable profile from detected hardware.
6. Generate into a durable resumable job with bounded concurrency.
7. Check every WAV for completeness, repetition, clipping, artifacts, abnormal
   silence and pacing. Apply only typed bounded repairs, then one configured
   offline fallback backend.
8. Give every residual failure an explicit live-TTS route instead of blocking
   the chapter.
9. Atomically publish and activate an incremental local game pack.

The quality router may use source-aware ASR/alignment and audio metrics, but it
must be calibrated against held-out human decisions before it can silently
accept or reject a class of outputs. Until a classifier is calibrated, the safe
terminal route is a bounded fallback or live synthesis, not mandatory line-by-line
review by the player.

## Progress and recovery

Before generation, the wizard shows selected chapters, dialogue-line count, an
estimated duration and disk requirement, and how many voice decisions remain.
During generation it shows player-level coverage such as `Prepared`, `Original
game voice`, `Will use live voice`, and `Remaining`; it does not expose mutable
authoring states. Slow work runs in the background without freezing navigation.

The job identity includes the imported story content and generation controls.
Restarting VNTTS resumes completed hashes rather than regenerating them. Adding a
chapter creates incremental work and reuses compatible voice decisions, source
audio and WAVs. Cancellation leaves the last fully published pack active. A new
pack replaces it only after full preflight and an atomic settings update.

The initial selection boundary stores `job.json` under the application data
directory. Its deterministic identity binds the exact story-index SHA-256 and
ordered selected story IDs. It also stores the exact selected line IDs and a
player-level estimate. Reopening the same content restores the newest compatible
selection; damaged or conflicting state is ignored for discovery and rejected
for explicit resume. Known discovery locations are bounded to the configured
story index and the extractor's platform application-data output. A file chooser
is the recovery path; VNTTS never scans arbitrary user directories.

Reverse: 1999 import runs behind a bounded subprocess adapter. VNTTS resolves a
packaged `r1999extractor.bootstrap` module or the `r1999-bootstrap` executable,
passes an exact application-owned output directory, and consumes only the
resulting shared `vntts.story-index` artifact. It never parses extractor-private
files or invokes a shell. Import runs outside the UI thread. Cancelling signals
and terminates only that exact child process; a nonzero exit is reduced to the
last actionable error line, and a missing output is rejected. The remaining
fallback asks for one game-installation folder, searches only beneath that
explicit root, resolves the resource, config and English bank directories, and
passes all three exact paths to the same importer.

`reverse1999-extractor` is an exact-revision runtime dependency. macOS and Windows
PyInstaller specifications collect its modules, data, binaries and distribution
metadata. A frozen VNTTS executable relaunches itself with a hidden provider
worker argument instead of assuming that the bundled application is a general
Python interpreter. The worker runs before Qt is created, so importer failures
cannot create a second dashboard or tray process.

Voice routing is planned in a separate atomic `voice-plan.json` beside the
selection job. The planner reopens the checksum-bound story index, excludes
original and non-speakable lines, and groups remaining lines by canonical voice
character plus exact portrait, age, source-bank and source-voice evidence. It
resolves explicit saved assignments and exact manifest names/aliases before any
audition; narrator dialogue and named roles without a usable candidate receive
an explicit narrator route without asking the player to confirm an already-known
absence. Each group records ordered line IDs, one representative phrase, the
chosen source and reference hashes. Its control digest depends only on that
group's selected voice and synthesis settings, so changing an unrelated voice
reference does not invalidate compatible work. Future player decisions use a
separate group/evidence/control digest and are never inferred from filenames or
mutable paths.

The potentially large story-index read and reference hashing run outside the Qt
thread while the selection dialog remains visibly busy. Cancelling requests a
cooperative stop and keeps the dialog open until that exact worker reaches a
terminal result; a cancelled or failed plan never closes as if it succeeded.

## Acceptance gates

The first production slice is complete only when an offscreen and synthetic
backend journey proves all of the following:

- fresh settings reach generation from one game/install selection;
- the user sees no mandatory configuration beyond chapter and ambiguous voice
  choices;
- cancellation and process restart resume without duplicate generation;
- every selected dialogue line ends as source audio, generated audio, explicit
  live fallback or intentional omission;
- no per-line listening is required on the default path;
- a verified local game pack is published and activated atomically; and
- the existing expert authoring tools can still explain every automatic route.
