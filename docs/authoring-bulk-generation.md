# Resumable bulk voice generation

`vntts.authoring.bulk_generation` owns the game-independent executor that turns
a validated `vntts.voice-generation-queue` v1 document into reviewed PCM WAVs.
It calls only `render(SynthesisRequest)` with `cache_policy=bypass`; generation
never opens a playback device or creates a discard audio sink.

## State is authoritative

New output uses `vntts.authoring-generation-state` version 1. The reader also
accepts preserved `r1999.bulk-generation-state` version 1 documents and keeps
that schema when resuming them. Queue identity is the SHA-256 of the exact raw
queue bytes. A different queue, unknown queue ID, changed line/text identity,
unsafe path, missing WAV, changed file hash, invalid PCM or changed stored
quality blocks resume rather than silently regenerating or overwriting work.

The legal item states are:

| State | Review | Meaning |
| --- | --- | --- |
| `failed` | absent | sparse failure that can be retried |
| `generated` | `pending_review` | valid WAV awaiting a decision |
| `generated` | `rejected` | valid WAV intentionally unpublished |
| `approved` | `approved` | valid WAV included in the derived manifest |

Legacy sparse failures keep their queue IDs, cumulative attempts, seed and last
error. Successful legacy records keep their line/text hashes, path, file hash,
provider/model/prompt provenance, quality and review decision. Rejected audio is
not regenerated automatically and can be approved later.

Before each render, `active` is atomically persisted with the queue/line/voice,
phase, local and cumulative attempt numbers, seed, provider/model/profile,
exact synthesis-text hash, unapplied queue-annotation hash, timestamps and
latest error. A stale active record is preserved under
`interrupted_attempts` and counted as a consumed attempt, so resume advances to
the next deterministic seed rather than repeating an uncertain render.

## Publication and recovery

Each attempt renders typed PCM into an adjacent partial WAV, validates PCM16
mono at 16 kHz or higher, a 0.1-180 second duration, non-silence and no clipping,
then hashes it before atomic publication. Only `completion=complete` with
matching backend/seed/profile diagnostics can publish. An additional frame
analysis rejects excessive leading, trailing or internal silence and outputs
whose silent-frame ratio exceeds 50%. Limited, cancelled, invalid and failed
attempts never become generated audio. Known failure removes only its
temporary partial; a pre-existing orphan or stale partial is moved into the
output's `interrupted/` directory before retry.

The queue is parsed from one exact captured byte snapshot. The CLI captures the
voice manifest, every resolved reference and a local model file or complete
model-directory tree before constructing the backend. Their exact SHA-256
values form `synthesis_provenance_sha256` and are rechecked after each render
and before manifest publication. Remote model IDs cannot provide byte
provenance; their configured ID and backend-reported model identity are recorded
instead. Queue delivery/prompt annotations are not currently inputs of
`SynthesisRequest`: state records their canonical hash separately, sets
`prompt_applied=false`, and uses the SHA-256 of an empty prompt rather than
claiming that an unapplied annotation changed audio.

An exclusive `.generation-lease.json` prevents concurrent writers. A live PID
in that lease, or in a colocated preserved pregeneration `job.json`, blocks the
run. A dead lease is preserved under `interrupted/` and the run may resume. If a
process stops after WAV publication but before state publication, the orphan is
preserved and a later attempt uses the next seed. If it stops after state but
before manifest publication, the manifest is simply rebuilt from state.

`manifest.json` is always a derived `vntts.generated-audio` version 1 projection
of only `approved` + `approved` records, sorted by `(line_id, text_sha256)`.
Missing or stale manifests do not override review state. Approval/rejection
writes state first and then rebuilds the manifest; a rebuild error reports that
the review decision was already saved and can be recovered with `publish`.
Workbench decisions additionally carry the displayed queue, state-item, state
and WAV SHA-256 snapshot into this transaction. The replacement state and
derived manifest are fully validated and staged under unique temporary names.
Under the exclusive lease, all four identities and the complete lease document
are checked again immediately before the canonical state is replaced; the lease
is checked once more before the manifest replace. If ownership changes in that
narrow interval, the durable state decision is reported as saved while the
older derived manifest remains fail-closed until recovery.
Additive raw entry fields retain generation profile, synthesis/control hashes,
voice, text transform and silence measurements. The pinned vntts-artifacts
0.6.2 lossless generated-audio document API exposes those extensions to typed
consumers. Generation state remains the mutable review and retry authority;
the generated manifest is still a derived approved-only projection.

## Command line

Resume generation through a configured typed backend:

```sh
uv run vntts-pregenerate generate \
  --queue /path/to/generation-queue.jsonl \
  --output /path/to/generated-audio \
  --voice-manifest /path/to/voice-manifest.json \
  --backend moss-tts \
  --model /path/to/local/model \
  --narrator-character Matilda \
  --generation-profile stable \
  --retries 2 --seed 0
```

`--limit` is applied to the candidate slice before already-completed items are
skipped, matching legacy resume semantics. `--character` can be repeated, and
`--include-prefer-source` explicitly opts recoverable source-audio records into
generation. The CLI skips characters without configured local references and
legacy pure `*sound effect*` lines. For MOSS only, one/two-word trailing
ellipses are transformed to a terminal period; both the original queue text
hash and exact transformed synthesis-text hash are persisted. Negative limits
or retries are rejected.

Review, recover a manifest and inspect state:

```sh
uv run vntts-pregenerate review \
  --state /path/to/generated-audio/generation-state.json \
  QUEUE_ID approved

uv run vntts-pregenerate publish \
  --state /path/to/generated-audio/generation-state.json

uv run vntts-pregenerate status \
  --state /path/to/generated-audio/generation-state.json \
  --queue /path/to/generation-queue.jsonl
```

Config-addressed resume workspaces use repeated `--queue-id` arguments for an
exact focused retry. Their child process also supplies expected voice-control
hashes and the output directory identity; a changed reference or directory swap
therefore fails instead of silently changing synthesis provenance. The
role-bound narrator selection is part of the control inventory.

Failure repairs add exact-ID-only controls:

- `--sentence-segment-failed QUEUE_ID` renders only safe complete-sentence
  segments with successive seeds and a bounded pause before concatenation;
- `--trim-edge-silence-failed QUEUE_ID` removes only excess measured leading or
  trailing silence before the normal quality gate.
- `--bounded-seed-failed QUEUE_ID` permits only a current typed missed-EOS
  failure and never exceeds three cumulative attempts for that item.
- `--inline-pause-failed QUEUE_ID` performs one MOSS-only comparison using a
  derived, hash-bound inline pause prompt; it requires `--retries 0`, preserves
  the original queue text hash and does not authorize a cohort rollout.
- `--offline-fallback-failed QUEUE_ID` moves an exhausted, provenance-bound
  failure to the configured typed offline fallback without relabeling history.

The flags are workspace configuration, synthesis-control provenance and state
provenance, not ad-hoc transforms. Their set must exactly match `--queue-id`,
the current state must still contain a typed matching failure, and every
repaired artifact remains pending manual review. Use
`failure-repair-plan` first; it is a read-only selector and never runs a model.

The headless projection and safety contract are documented in
[authoring-workspaces.md](authoring-workspaces.md). The Qt workbench projects
that attempt into a live elapsed-time display without persisting derived time.

After a dedicated selected queue has complete terminal review coverage, use
the [final game-pack publication boundary](authoring-game-pack-publication.md)
to stage and checksum-bind the story, voices and approved generated audio. The
publisher does not rewrite this authoritative state or its source manifest.
