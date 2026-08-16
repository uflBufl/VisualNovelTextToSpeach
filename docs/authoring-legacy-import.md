# Non-destructive legacy authoring import

VNTTS keeps offline pregeneration work in the isolated `vntts.authoring`
package. The primary player does not import extractor modules or start legacy
generation commands. The separate `vntts-pregenerate` entry point supports
read-only discovery and non-destructive import of existing Reverse: 1999
pregeneration jobs, explicitly paired standalone generation outputs, and
blind-listening preservation snapshots.

## Supported legacy documents

The importer reads the following current contracts without importing
`r1999extractor`:

- `r1999.pregeneration-job` version 1 `job.json`;
- `vntts.voice-generation-queue` version 1 JSONL through the shared
  `vntts-artifacts` v0.6 reader, including producer extension fields and older
  records that omit newer optional source-audio fields;
- `r1999.bulk-generation-state` version 1 from the job output directory;
- an optional `vntts.generated-audio` version 1 manifest through the shared
  reader.

Generation state is the authority for attempts, seeds and review decisions.
`generated` plus `pending_review`, `approved` plus `approved`, and `generated`
plus `rejected` remain distinct. A generated-audio manifest is only the derived
approved projection. A missing or stale projection does not turn an approval
into a rejection. When a manifest is older than authoritative state, the
importer preserves it at `legacy/stale-generated-audio-manifest.json`, records
specific diagnostics, and does not expose it as the current approved
projection. Manifest path escapes, unreadable WAVs and audio checksum failures
still reject the import. An `active` attempt is preserved as diagnostic
provenance but is never treated as completed work.

The importer verifies exact queue IDs, line IDs and text hashes; the state's
full queue SHA-256 binding; state-to-queue identity; review status; generated
WAV containment, PCM format and checksum; and every published manifest entry
against its state provenance. Provenance disagreement marks the projection
stale; absolute, parent-relative, backslash and symlink-escaping generated
paths, unreadable audio and checksum failures reject the import with an
actionable error.
Legacy executable and model fields are retained only as provenance and are
never executed. Historical version 1 jobs with an omitted or null model and
targets with an omitted or null `episode_count` remain readable.

Standalone import never guesses from filenames, timestamps, directory names or
truncated hashes. The caller must select both the queue and output directory.
The output is accepted only when `generation-state.json.queue_sha256`, or a
manifest-only snapshot's `source_queue_sha256`, equals the SHA-256 of the exact
queue bytes. A colocated state remains authoritative and a stale manifest is
quarantined under `legacy/` exactly as it is for job-backed import. Different
outputs bound to the same queue remain separate histories because canonical
output location is part of logical identity. Manifest-only outputs are kept as
unconfirmed snapshots because they cannot prove review authority.

Blind-listening preservation accepts the version 1 session, hidden key and
optional report contracts. It verifies the key's exact checksum binding,
session/key source identity and source-report hashes, unique models and trials,
complete non-self A/B assignments, ratings/progress, relative alias
containment, alias-to-assigned-source audio hashes, and a recomputed report
apart from its generation timestamp. Every alias receives its own SHA-256 in
the private import inventory because the legacy session did not store alias
hashes. The hidden `.blind-key.json` bytes and file mode are retained; key
assignments are not copied into a public summary.

## Application-data layout and safety

By default imports are written beneath the platform application-data directory
at `authoring/legacy-imports/`. Files are first copied into a private staging
directory and checksum-verified, then the complete directory is renamed into
place. The source job is never deleted, rewritten or regenerated. Existing
imports are never overwritten.

Immediately before staging is renamed, every planned source artifact is hashed
again: control documents and every generated or blind-listening WAV. A changed
file aborts and removes staging with an actionable retry-when-idle error. A live
legacy PID is rejected before copying. A job marked `running` is accepted only
when its recorded positive PID is proven absent; its raw status is preserved,
the imported snapshot is classified as `interrupted`, and discovery exposes the
diagnostic. Missing, invalid, live, permission-denied or otherwise uninspectable
PIDs fail closed. This closes the source-mutation window without locking or
modifying producer files.

Each import contains `import.json`, the original job snapshot, the exact queue,
the state and optional generated manifest, plus every validated generated WAV.
`import.json` records source paths and checksums, external story/voice
provenance, full queue identities, attempts, seeds, statuses, review decisions
and generated-file provenance.

Logical import identity uses the full queue checksum and canonical legacy
output location; the source fingerprint binds the exact authoritative state
snapshot. Original and `registered_existing_job` wrappers for the same
queue/output are idempotent. Re-import verifies every existing destination
checksum. A changed source at the same logical location or modified destination
fails without overwriting. Separate output histories may preserve the same
canonical queue item while carrying different attempts, review decisions,
providers or generated WAVs. Cross-history collision checks compare immutable
line ID, text SHA-256 and the canonical full queue-record digest, so a changed
voice, action or producer extension remains a hard conflict instead of being
merged or accepted last-write-wins.

## Commands

Discover compatible and incompatible jobs without writing application data:

```sh
uv run vntts-pregenerate discover-legacy \
  --jobs-root /path/to/pregeneration-jobs
```

Import one selected job:

```sh
uv run vntts-pregenerate import-legacy /path/to/job-directory
```

Inspect, then import one explicit standalone pairing:

```sh
uv run vntts-pregenerate inspect-standalone \
  --queue /path/to/queue.jsonl --output /path/to/generated-audio
uv run vntts-pregenerate import-standalone \
  --queue /path/to/queue.jsonl --output /path/to/generated-audio
```

Inspect, then preserve one listening session:

```sh
uv run vntts-pregenerate inspect-listening /path/to/listening-session
uv run vntts-pregenerate import-listening /path/to/listening-session
```

All commands emit JSON. Discovery retains incompatible jobs with an actionable
`compatibility_error`. Import can be redirected to a test or portable location
with `--destination-root`.

Discovery reports standalone generation queues and output directories but does
not guess a pairing; its error points to the required explicit inspection
command. Valid `r1999.model-listening-session` directories are reported as
`preserve-ready`. Discovery and inspection never write source or application
data.

This slice does not resume generation, rebuild a derived manifest, alter review
decisions, or migrate generic queue-building and model-selection workflows.
Those remain explicit backlog items until the authoring runtime owns and tests
them end to end.

## Verified legacy census

The read-only discovery gate was repeated on 2026-08-17 and validated all three
current job-backed Reverse: 1999 shapes as compatible immutable snapshots. The
registered Patch 3.7 job has 1,220 queue items and 680 generated results. Its
source still says `running`, but recorded PID 49573 is proven absent, so it is
reported as `interrupted` without changing the source. The stopped newer
character-story history has 592 queue items, 197 generated-pending-review
results and 141 valid sparse failures. The separate older nullable-target
history has the same 592 canonical queue records and 338 approved results;
different review/generation provenance no longer causes those distinct output
histories to collide. No source or VNTTS application-data file was written
during this discovery census.

The extended read-only gate surfaced two unpaired queues (63,419 and 97,893
items), three archived output directories, and the current blind-listening
session. Explicit inspection of the full-SHA-bound Patch 3.7 quarantine pair
validated 1,220 queue items and its distinct 1,158-approved state/manifest
history without importing it. The quarantine directory is never selected
automatically. Its merged manifest has no source queue SHA-256 and is rejected
as unbound historical data rather than guessed into a resumable history.

The listening dry-run validated all 45 rated trials, six models, 45 hidden A/B
assignments and 90 relative WAV aliases, including the legacy `dimensions`
form, exact key binding, source-report hashes and recomputed report. No legacy
audio or application data was copied during either dry-run.
