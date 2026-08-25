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

Version 2 import snapshots preserve the source job's validated, timezone-aware
`created_at` and optional `updated_at` inside `legacy_job`. A malformed or
timezone-naive timestamp, or an update earlier than creation, is rejected
instead of being presented as history.
Version 1 immutable imports that predate these fields remain readable and
idempotent; their missing source times stay explicitly unavailable and are
never inferred from filesystem modification times.

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

The common destination policy lives in the dependency-free
`authoring.import_paths` module. Both generation-history and blind-listening
importers consume it, while generation discovery may inspect listening
snapshots without creating an importer cycle. `legacy_import.default_import_root`
remains an imported compatibility symbol, so existing Python callers keep the
same path and behavior.

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

Import commands never resume generation, rebuild a derived manifest, alter
review decisions, or publish a final pack. Those are separate explicit
authoring operations, so preserving a snapshot is not evidence that it has
resumed successfully.

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

## Verified application-data migration

The three stable job-backed snapshots were imported from clean commit
`fb6452a` on 2026-08-17. Destination names are deterministic immutable import
identities; they contain no source-directory component:

| Destination identity | Queue | Authoritative state | Derived manifest |
| --- | ---: | --- | --- |
| `legacy-12888f0d08ffe96b5be29f7b` | 592 | 338 approved | 338 current entries |
| `legacy-395a5e5eec0327a3a793b66d` | 592 | 197 generated pending review, 141 failed | 0 current entries |
| `legacy-14d28505d16f4729c363c2de` | 1,220 | 680 generated pending review | absent |

The first history already existed and returned `created=false`. The other two
returned `created=true` once and `created=false` on an immediate repeated
import. The two 592-item histories coexist because their immutable queue-record
digests agree even though their state, review and synthesis provenance differ.

For each source, verification computed SHA-256 over the ordered inventory of
artifact role, source identity and individual artifact SHA-256 before and after
the imports. The digests were unchanged:

- older 338-approved history:
  `e009b442d9d38919dca21b7bb3dfae3aada35e8969f0d877f0f08b71d5a8c5ab`;
- newer generated/failed history:
  `e6e25155811244def7984f7f2f3bea4ae52375f9802a3a1b4bed01593d8bccb2`;
- interrupted Patch 3.7 history:
  `cf4e75a056147e38e620d621335bc0632cb5aae09b1fa0700de47873830e3009`.

The preserved blind-listening import is
`listening-4cc961e3ab2dba5492a879b6`: 45 of 45 trials rated, six hidden models,
90 audio aliases, and the report present. All 93 imported artifacts and all 93
corresponding source artifacts still match their recorded SHA-256 values. Its
source fingerprint is
`1c043b4971d05354becb6ed7cf758cd1fdb038835a6e44c90fd83c932398d23b`.

Migration completion does not clear the operational gates. The 680 Patch 3.7
items and 197 newer-history items still require manual review. The 141 failed
newer-history items require an explicit retry decision after their failure
cause and voice readiness are reviewed. Remaining queue items require the
normal preflight filters for missing references, recoverable source audio and
sound effects. A first successful VNTTS resume must prove cumulative attempts,
seeds and immutable queue identity before the preservation TODO can be removed.
