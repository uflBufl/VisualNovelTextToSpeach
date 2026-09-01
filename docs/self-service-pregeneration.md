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
then compares the two strongest viable candidates using the same synthesized
phrase. Each candidate has independent play/replay and choose controls. The
player can also choose `Neither sounds right`, `Choose for me`, or `Choose all
automatically`. Failed previews and cases with only one viable candidate are
resolved automatically instead of being presented as fake decisions.

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

Before an ambiguous A/B card appears, every exact source reference passes the
checksum-bound reference preflight and every generated preview passes the same
format, clipping and speech-silence gates as bulk generation. A failed candidate
is omitted automatically; one surviving candidate is selected without asking,
and zero surviving candidates take the existing safe fallback. Diagnostic
artifact signals are not rejection authority until held-out evidence establishes
a safe margin.

The first comparison uses one representative line. When the same voice remains
ambiguous, `Try another phrase` lazily renders one different line from the same
character variant under identical synthesis controls. The second phrase is
optional, has its own checksum-bound cache identity and never adds a mandatory
decision.

Voice planning normalizes stale synthesis controls before any preview or bulk
work starts. Pocket always uses its native `default` profile; invalid clone-model
profiles fall back to `stable`; and MOSS selected on non-Apple-Silicon hosts
falls back to Pocket instead of starting a backend that cannot run there.

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

After chapter selection, the same bounded importer worker prepares only the
missing named roles that have exact installed source audio elsewhere in the
story index. It reuses the checksum-bound bank routes and technical reference
preflight already owned by `reverse1999-extractor`, keeps at most three
recommended references per evidenced portrait/bank group, and publishes their
voice IDs, bank, source lines, event IDs, duration, quality score and WAV hashes
in an identity-addressed voice manifest. The manifest is bound to the exact
story-index hash and is reused on restart. VNTTS validates the manifest, report
and reference hashes before adding those candidates to the ordinary A/B voice
card. If preparation finds no safe candidate or fails, pregeneration remains
playable through the existing narrator route instead of opening an authoring
workflow.

Candidate-manifest v2 also binds each available exact portrait PNG. The
extractor resolves the installed Unity `Sprite` by portrait ID, writes it under
the imported content's `portraits/` directory, and caches the source bundle
against the story-index hash. Known content-addressed head-icon shards avoid a
full game scan on the current release; scanning all installed bundles remains a
fallback for a future shard layout. Voice planning rejects a changed or stale
PNG before the existing A/B card renders it. A real 11-portrait extraction
reproduced the previously reviewed PNG bytes exactly and took 0.86 seconds via
the direct shard path on the development Mac.

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
also validates exact queue-to-voice bindings carried by reviewed source-reference
manifests. Exact queue bindings split otherwise similar groups and remain
automatic. The candidate inventory then combines that authority with exact
manifest names/aliases, imported installed-game candidates and reviewed variants
of the same character. Its stable
evidence order is: explicit assignment or exact queue binding, matching original
voice ID, matching portrait plus source bank, exact character name or alias,
matching portrait, matching bank, then another reviewed character variant.
Candidates outside the top evidence margin remain in diagnostic provenance but
do not create another player prompt. A unique candidate, a clear evidence winner
and a one-line incidental role resolve automatically.

Resolved voices do not receive a separate synthetic preview corpus: their actual
selected story lines already exercise the chosen voice and every resulting WAV
passes the generation quality gates. Adding a second artificial corpus would
delay preparation without strengthening publication. Player listening remains
limited to materially ambiguous voice choices; expert diagnostics remain
available outside the default workflow.

Narrator dialogue and named roles without a usable candidate receive an explicit
narrator route without asking the player to confirm an already-known absence.
Each group records ordered line IDs, one representative phrase, the complete
ranked inventory, the bounded comparison candidates, the chosen source and
reference hashes. Its control digest depends only on that group's selected voice
and synthesis settings, so changing an unrelated voice reference does not
invalidate compatible work. Player decisions use a separate digest containing
only materially competitive candidates; adding a dominated candidate therefore
does not force the player to repeat a choice. A changed selected reference or a
new credible competitor does.

Every usable selected voice is also represented as a checksum-bound candidate
inside its group. This candidate inventory is the boundary used by the
ambiguity detector and player card; a preview request cannot introduce an
arbitrary manifest voice. The audition renderer accepts only a candidate from an
unresolved `needs-audition` group, reopens the exact manifest and reference
hashes, and synthesizes the group's single representative phrase with the
planned backend, model, profile and deterministic seed where supported. Its WAV
is atomically cached by all of those controls and reused across restarts. Model
startup and rendering are cooperative-cancellation boundaries; cancellation,
limited output, stale references and mismatched diagnostics publish no preview.
Embedded Pocket voices follow the same contract without pretending that a
reference file or seed exists.

An unresolved plan hides the chapter-selection surface and shows one stable A/B
comparison. The header reports how many choices remain, estimates their duration
and offers `Choose all automatically`. The card explains the character variant,
affected line count and reuse scope. It renders an exact installed portrait from
the content package when `portraits/<portrait>.png` exists and still matches its
planned checksum; it never substitutes a portrait ID as if it were an image.

Both candidates synthesize the same representative phrase. Each has independent
play/replay and choose controls, while a plain-language recommendation explains
the evidence behind the first candidate. When a checksum-bound source-reference
candidate provides a playable original WAV, `Play original game voice` turns the
question into a similarity comparison. Without that anchor the copy explicitly
calls the decision a preference and offers `Choose for me`. Playback never moves
or disables the decision controls.

`Neither sounds right` advances through remaining technically rendered
candidates and then previews the configured narrator with the same phrase. If no
narrator is runnable under the current offline setup, VNTTS keeps the best viable
candidate rather than recording a route that would strand the job. A failed
candidate preview is never displayed as a choice: one remaining result is chosen
automatically and zero results take the safest runnable fallback. The choices
advance immediately and are written together atomically in the background; a
failed write retains them in memory and exposes one retry action. `Change saved
voice choices` on the chapter-selection screen intentionally ignores prior
decisions for that run, while the normal path reuses them.

A persisted choice is accepted only when it is a candidate bound to that group
or the narrator sentinel. After all cards are decided, the wizard rebuilds the
same voice plan and proceeds only if no ambiguity remains; a stale or
inapplicable saved choice therefore cannot leak into generation. Cancelling
during preview waits for the exact worker to reach a terminal state before the
dialog closes.

The potentially large story-index read and reference hashing run outside the Qt
thread while the selection dialog remains visibly busy. Cancelling requests a
cooperative stop and keeps the dialog open until that exact worker reaches a
terminal result; a cancelled or failed plan never closes as if it succeeded.

Generation consumes an identity-addressed private input directory rather than
the mutable source files directly. VNTTS copies only the selected story records,
reopens every planned reference through the manifest ownership guard, verifies
its planned hash and decodes it to a PCM16 mono WAV. It writes an effective
manifest whose character names reflect the resolved plan: an assigned character
voice is bound under that requested character, while all explicit fallback roles
share one `Narrator` entry. The shared queue builder then creates the exact
selected-story queue with unresolved source-audio status kept as `resolve_audio`,
not guessed. `input.json` binds every resulting artifact hash. Repeating the same
job and controls reuses that immutable directory without rewriting it; changed
references, conflicting variants, cancellation and a missing narrator reference
fail before publication.

If the imported story carries source-audio semantic evidence, private input
materialization does not retain the full-import ledger unchanged. It selects
only evidence entries and source-line bindings used by the chosen stories,
recomputes the evidence identity and checksum, rewrites selected line bindings,
and validates the projected story/evidence pair. The optional evidence path and
hash then become part of the immutable input contract, allowing a partial local
pack to be verified independently of unselected chapters.

Pocket TTS is the one reference-free exception. Its allowlisted embedded voices
are immutable model inputs rather than guessed files, so the effective manifest
may bind a character or `Narrator` to an allowlisted Pocket speaker with an empty
reference list. The offline CLI treats that speaker as runnable and records it
through the manifest control instead of attempting to hash the preset name as a
path. With the default Pocket backend, a user who has no character voice pack can
therefore prepare a selected story entirely with embedded `alba` narrator speech.
MOSS and every cloning route still require real checksum-bound reference audio.
Pocket's effective generation profile is always its supported `default` profile,
even when an older saved application setting or resumable plan says `stable`.
This keeps the first pass and the exact automatic live-fallback evidence on the
same provider/model/profile identity; otherwise one failed Pocket line could
block finalization despite all audio work having completed.

The selection dialog runs voice planning and private-input publication as two
consecutive background phases before reporting success. Content and selection
controls remain disabled during either phase, while one visible Cancel action
cooperatively stops the active phase and closes only after its worker terminates.
The dashboard completion message reports matched voice groups, narrator
fallbacks and the exact number of runnable generation lines; it no longer claims
that voice matching is an unspecified future manual step.

Offline rendering runs in one owned subprocess against that exact private input.
The command binds queue, effective manifest, backend, model, profile and each
narrator-fallback role; Pocket receives its required single unseeded attempt,
while cloning generation keeps the bounded provider retry count. Reusing the same
input identity resumes an adjacent identity-addressed output directory and the
bulk generator's atomic state without mutating its inputs. The parent drains both
output pipes while polling, terminates only its child on cancellation, validates
the published state/manifest and reduces terminal outcomes to generated, failed
and other terminal counts. Frozen packages relaunch the app through a hidden
generation-worker argument that dispatches before Qt is created.

Different selection-job identities share one application-owned synthesis cache.
Normal pregeneration reads and writes it, while an explicit regeneration refreshes
the matching entry and legacy expert generation without a configured cache remains
isolated. Cache identity includes backend, model, normalized text, voice, reference
content, profile, seed and backend controls; copied references with identical bytes
therefore remain reusable across newly selected chapters, while changed bytes cannot
alias an older waveform. The disk cache is sized from the current selection without
inflating the much smaller in-memory playback cache. Generation state, quality
validation and game-pack publication remain independent and immutable for every job;
a cache hit supplies PCM, not a prior line decision or manifest entry.

After the first pass, the player workflow derives a fresh failure-repair plan
from the checksum-bound queue and generation state. It batches only deterministic
safe actions: resume an interrupted item once, split at validated complete
sentence boundaries, trim excessive edge-only silence, or spend the remaining
bounded MOSS seed attempts. Pocket is unseeded, so it never receives a seed
retry. Every subprocess is scoped to the exact failed queue IDs and one repair
strategy; successful and unrelated outcomes are not regenerated. The plan is
recomputed after every batch, and one queue/action pair is never repeated in the
same recovery run. A residual failure with bound non-Pocket provenance receives
exactly one Pocket default fallback attempt. A failed Pocket result is never
retried by Pocket again; it proceeds to the explicit live route below. Legacy
failures without bound synthesis provenance remain blocked instead of being
guessed. Recovery shares the generation cancellation signal and resumes from the
same output after restart.

When Pocket itself cannot finish a line and no supported safe action remains,
the same background recovery phase records an explicit live-Pocket terminal
route. This is not a fabricated successful WAV: schema-versioned evidence embeds
the exact failed outcome, its hash, typed failure kind, queue hash and deferred
recovery action. State validation rejects interrupted outcomes, stale evidence,
non-Pocket controls or an attempt to bypass a still-supported safe repair. The
wizard can therefore finish without line-by-line review while diagnostics and a
future game pack still distinguish prepared audio from runtime synthesis.

Every generated WAV has already passed the bulk generator's file-integrity and
speech-quality gates. The default wizard snapshots all still-pending WAVs from
one immutable state revision, revalidates their checksums, and accepts them in
one atomic cohort transaction tagged as an automatic, non-human technical-gate
decision. Authority collection reads the generation state once even for a large
chapter, and the work remains off the Qt thread. This removes mandatory per-line
approval without weakening the fail-closed manifest publication boundary;
expert cohort review remains available outside the player workflow.

Final publication is also a background, identity-addressed operation. It
requires every selected generation item to be either automatically approved or
an explicit live-Pocket fallback, then copies only the selected story, effective
voice references, approved WAVs and optional projected ASR evidence into a new
portable game-pack directory. The generated-audio manifest keeps generated WAVs
and live fallbacks as separate routes. Before the directory can become visible,
the public game-pack importer, semantic-evidence validator and runtime generated
audio loader all reopen the staged bytes. Publication uses a no-overwrite atomic
rename; repeating an identical terminal state reuses the same validated pack,
while a changed state produces a new identity and cannot mutate the earlier
pack.

Activation is a failure-atomic runtime transaction. VNTTS preflights the
published manifest again, stops an already-loaded speech runtime, applies the
pack with `prefer-generated`, and proves that the runtime starts before writing
the new settings. If runtime startup or settings persistence fails, it restores
the previous settings and restarts the previous runtime. An application shutdown
cancels the transaction without restarting speech during shutdown. Only after
the transaction succeeds does the dashboard publish the new settings and report
that offline audio is active.

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

The zero-ambiguity synthetic acceptance journey exercises this chain with the
real job, voice-plan, private-input, recovery, acceptance, publication and
activation components. One Pocket-style render succeeds and one reaches its
typed limit; the latter becomes an explicit live fallback. The test then opens
the completed result through the desktop action, saves `prefer-generated`, and
verifies that the pack is active without exposing authoring vocabulary or asking
for per-line review. A second synthetic journey now interrupts the application at
a real source-evidence ambiguity, reopens the same resumable job, renders the A/B
comparison, persists one candidate decision, replans to zero ambiguity and
publishes the pack without per-line review.
