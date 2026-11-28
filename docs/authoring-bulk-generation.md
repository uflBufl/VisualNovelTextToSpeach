# Resumable bulk voice generation

**Status:** Current expert workflow. For the player workflow, see
[self-service pregeneration](self-service-pregeneration.md).

`vntts.authoring.bulk_generation` owns the game-independent executor that turns
a validated `vntts.voice-generation-queue` v1 document into reviewed PCM WAVs.
It renders through the typed `render(SynthesisRequest)` boundary and never
opens a playback device. `--cache-directory` enables the shared content-addressed
synthesis cache; without it, expert runs use an isolated temporary cache.

## State is authoritative

The sole working-state authority is `generation-state.sqlite3` in the output
directory, owned by `vntts.generation_database`. The semantic state document is
still `vntts.authoring-generation-state` version 1, but the working file is
SQLite, not JSON. Renaming an old JSON file does not migrate it. Existing output
without its registered database is rejected; immutable legacy imports are a
separate preservation boundary described in
[legacy import](authoring-legacy-import.md).

Queue identity is the SHA-256 of the exact captured queue bytes. Unknown queue
IDs, changed line/text identity, unsafe paths, missing or modified WAVs, invalid
PCM and inconsistent stored quality block resume.

| State | Review | Meaning |
| --- | --- | --- |
| `failed` | absent | typed failure eligible only for supported repairs |
| `generated` | `pending_review` | valid WAV awaiting a decision |
| `generated` | `rejected` | reviewed WAV excluded from publication |
| `approved` | `approved` | valid WAV eligible for package publication |
| `live_fallback` | `live_fallback` | explicit runtime route, without a successful WAV |
| `omitted` | `omitted` | explicit pure-event omission, without fabricated audio |

Cancellation remains resumable work; it is not successful generation or an
exhausted failure. Attempts, interrupted work, synthesis controls, effective
voice, exact synthesis text and review provenance remain in state.

## Publication and recovery

Each attempt renders into an adjacent partial WAV and validates PCM16 mono,
format, duration, non-silence, clipping and speech-silence limits before atomic
WAV publication. Only typed complete output with matching requested diagnostics
may publish. Limited, cancelled and failed attempts never become generated
audio. Interrupted partial or orphaned audio is preserved for diagnosis rather
than treated as an approved result.

An exclusive `.generation-lease.json` prevents concurrent writers. PID and
process-start checks plus the persistent advisory guard serialize lease
creation, stale recovery and cleanup. Inaccessible processes remain
conservatively live. Resume uses durable state and bounded attempt history;
it does not infer completion from a WAV filename.

The queue, selected voice manifest, references and local model controls are
checksum-bound. Remote model IDs retain their configured and reported identity
without claiming local weight-byte provenance. Delivery annotations currently
remain metadata: `SynthesisRequest` does not apply them, and state records
`prompt_applied=false` with the empty-prompt digest.

Generation and review update SQLite transactions; they do not write an adjacent
`manifest.json`. There is no separate `publish` command to rebuild a working
manifest. Package publication derives the approved-only generated-audio index
from a stable database snapshot, copies the eligible WAVs and validates the
staged pack. Exported immutable state snapshots retain their own identity;
do not hash or copy a live SQLite database as if it were a JSON document.

Expert review binds the displayed queue, state snapshot, item and WAV digests.
The workbench requires the exact audio to reach end-of-media before enabling a
decision. Playback failure grants no listening credit. Under the generation
lease, the transaction rechecks the bound authority and commits the decision
atomically; changed authority requires refresh and replay. The default player
workflow instead records explicit automatic technical-gate acceptance, as
described in [self-service pregeneration](self-service-pregeneration.md).

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
generation. The CLI skips cloning voices without configured local references;
allowlisted embedded Pocket voices need no reference file. It also skips
legacy pure `*sound effect*` lines. Mixed inline directions such as
`N-No! *gurgle*`, `Wh-What! *gasp*`, and event-only `Tsk!` are also excluded
from ordinary TTS through the typed policy documented in
[`authoring-audio-events.md`](authoring-audio-events.md). They require an exact
reviewed composition; the marker is never sent as pronounceable text. For MOSS
only, one/two-word trailing ellipses are transformed to a terminal period; both
the original queue text hash and exact transformed synthesis-text hash are
persisted. Negative limits or retries are rejected. Pocket TTS does not expose
deterministic sampling seeds, so every Pocket generation run is restricted to
one attempt (`--retries 0`), sends `seed=None` to the backend and records
`seed_applied=false`. The integer state `seed` remains the monotonic attempt
identity used by authoring; it is not represented as an applied model seed.

Review and inspect the authoritative SQLite state:

```sh
uv run vntts-pregenerate review \
  --state /path/to/generated-audio/generation-state.sqlite3 \
  QUEUE_ID approved

uv run vntts-pregenerate status \
  --state /path/to/generated-audio/generation-state.sqlite3 \
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
the current state must still contain a typed matching failure. Expert repair
outputs remain pending review unless an explicit automatic acceptance path is
selected. Use `failure-repair-plan` first; it is a read-only selector and never runs a model.

The headless projection and safety contract are documented in
[authoring-workspaces.md](authoring-workspaces.md). The Qt workbench projects
that attempt into a live elapsed-time display without persisting derived time.

After a dedicated selected queue has complete terminal review coverage, use
the [final game-pack publication boundary](authoring-game-pack-publication.md)
to stage and checksum-bind the story, voices and approved generated audio. The
publisher does not rewrite this authoritative state or its source inputs.
