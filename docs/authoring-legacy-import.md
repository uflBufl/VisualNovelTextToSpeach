# Non-destructive legacy authoring import

VNTTS keeps offline pregeneration work in the isolated `vntts.authoring`
package. The primary player does not import extractor modules or start legacy
generation commands. The separate `vntts-pregenerate` entry point currently
supports read-only discovery and non-destructive import of existing Reverse:
1999 pregeneration jobs.

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

## Application-data layout and safety

By default imports are written beneath the platform application-data directory
at `authoring/legacy-imports/`. Files are first copied into a private staging
directory and checksum-verified, then the complete directory is renamed into
place. The source job is never deleted, rewritten or regenerated. Existing
imports are never overwritten.

Each import contains `import.json`, the original job snapshot, the exact queue,
the state and optional generated manifest, plus every validated generated WAV.
`import.json` records source paths and checksums, external story/voice
provenance, full queue identities, attempts, seeds, statuses, review decisions
and generated-file provenance.

Logical import identity uses the full queue checksum and canonical legacy
output location. Original and `registered_existing_job` wrappers for the same
queue/output are idempotent. Re-import verifies every existing destination
checksum. A changed source or modified destination fails without overwriting.
Across separate jobs, the same queue ID may coexist only when line, text, file,
attempt, seed, review and synthesis provenance agree; conflicting work is a
hard error rather than last-write-wins.

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

Both commands emit JSON. Discovery retains incompatible jobs with an actionable
`compatibility_error`. Import can be redirected to a test or portable location
with `--destination-root`.

Discovery also reports standalone generation queues, standalone state/manifest
directories, and `r1999.model-listening-session` directories. They are marked
unsupported rather than silently omitted. Their source files are not changed;
non-destructive import of those unpaired artifacts remains backlog.

This slice does not resume generation, rebuild a derived manifest, alter review
decisions, or migrate generic queue-building and model-selection workflows.
Those remain explicit backlog items until the authoring runtime owns and tests
them end to end.

## Verified legacy census

The read-only discovery gate on 2026-08-16 accepted all three current
job-backed Reverse: 1999 sources: the registered Patch 3.7 job with 1,220 queue
items and 680 generated results, the current character-story job with 592 queue
items and 180 generated results, and the older nullable-target job with 592
queue items and 338 generated results. No source or VNTTS application-data file
was written during that census.

The same gate surfaced two standalone queues, three standalone archived output
directories and the existing blind-listening session as unsupported instead of
silently omitting them. These artifacts explain why the broader preservation
TODO remains open even though all job-backed generation currently imports.
