# Final game-pack publication

`vntts.authoring.publish_final_game_pack` is the final, non-destructive
authoring boundary. It creates a portable `vntts.game-pack` version 2 directory
from a fully reviewed bulk-generation state plus the exact queue, story index
and voice manifest that produced it. The implementation uses only the released
vntts-artifacts v0.7.0 game-pack, binding and lossless generated-audio APIs;
review authority remains in generation state. The live-sequence component is
optional and is never inferred from story records alone; existing version-1
packs remain readable through the shared compatibility loader.

## Publication gates

Final publication requires all of the following:

- state is bound to the SHA-256 of the exact raw queue bytes;
- state contains exactly the selected queue IDs, with every item terminal as
  approved, rejected or explicitly authorized `live_fallback`; active, raw
  failed, pending or partial state is rejected;
- queue metadata contains source paths and SHA-256 bindings for its original
  story index and voice manifest. A legacy queue that predates those fields is
  accepted only through the explicit reviewed-waveform migration described
  below, and only when its selected story and voice SHA-256 values match the
  migration authority. A copied immutable story snapshot may replace a legacy
  path or pre-rebase hash only when the selected file hash matches the
  migration authority, which is itself bound to the validated base workspace
  and exact queue bytes. Publication never follows or trusts a later-mutated
  file at the old absolute path. Story identity must still match exactly. A deliberate
  replacement voice snapshot is allowed only when every publishable item proves
  that selected manifest through synthesis controls or the exact-waveform
  migration;
- every approved line ID/text hash exists in the bound story index;
- current-schema state contains the per-control inventory captured by bulk
  synthesis. The manifest and every referenced voice WAV must retain the same
  path and bytes used during synthesis. An exact approved WAV may instead carry
  a `vntts.authoring-reviewed-waveform-publication` authority. It binds the
  unchanged base result, line/text/WAV hashes, source workspace/state/queue,
  selected story and voice manifests, and current narrator references. It
  explicitly sets `synthesis_reproducibility=false`; it never reconstructs
  controls that were not recorded. A rejected WAV is not pack payload and is
  therefore excluded from synthesis-control validation;
- every generated WAV still matches authoritative state hashes, PCM quality and
  its approved review decision.

The original generated-audio manifest may be absent or stale. It is not the
review authority and is never rewritten by this command. A fresh approved-only
manifest is built inside staging from the exact state snapshot. Rejected audio,
partial WAVs, queue/state files and review diagnostics remain in application
data and are not shipped. The generated-audio metadata carries a checksum-bound
`vntts.authoring.live_fallback` ledger for every deliberate fallback identity.
It contains no audio and cannot be inferred from an absent or failed record.
An exact reviewed-waveform ledger is also copied into generated-audio metadata,
while the game-pack authoring extension records its batch ID, approved count and
non-reproducibility claim. Removing one approved identity from that ledger does
not grant a partial bypass: an uncovered approved item must still have its exact
synthesis-control inventory.

A rejected generated result receives live-fallback authority only through an
explicit immutable successor. Schema-v7 evidence embeds the complete unchanged
rejected result and binds the exact queue, base workspace/state, current
effective synthesis character, reference hashes and route authority. The
operation never changes the rejected status, publishes the rejected WAV or
claims that Pocket reproduces the rejected provider's timbre. Runtime validates
the evidence before requesting the recorded Pocket voice and fails closed on
route, result or reference tampering.

When the migration covers every shipped approved WAV, it is the final
publication authority for those payloads. The packager does not recursively
reopen the historical config-rebase graph after acquiring its own generation
lease: that graph was validated while creating the immutable migration, whose
ledger embeds its exact base workspace, state and results. Partial migrations
do not receive this exemption.

When a proven replacement voice snapshot is published, the raw game-pack
authoring extension retains the original queue voice-manifest SHA-256, the
selected SHA-256, an explicit override flag and the role-bound narrator
selection. It never rewrites the queue to pretend the replacement was its
original source.

## Atomic and non-destructive behavior

Story and voice documents are copied byte-for-byte into a sibling staging
directory. Referenced voice and approved generated WAVs are copied only through
safe contained POSIX-relative paths and rechecked by SHA-256. The shared writer
creates checksum bindings for every shipped artifact, and the shared loader
fully preflights the staged pack.

Immediately before commit, VNTTS rechecks the queue, exact state snapshot,
story, voice manifest and every copied source file, plus both generation and
publication lease ownership. Final-pack and terminal-conflict workspace
publication share the same generation-lease set and atomic no-replace
primitives. The staging directory is then renamed without replacement. macOS uses
`renamex_np(RENAME_EXCL)`, Linux uses
`renameat2(RENAME_NOREPLACE)`, and Windows uses its non-replacing rename
semantics. Publication refuses an existing file, symlink, empty directory or
populated directory; it never merges with or overwrites a previous delivery.
Unsupported filesystems/runtimes fail closed.

Once the no-replace rename succeeds, publication is committed. A later lease
cleanup ambiguity cannot turn that committed result into a reported failure.
Source manifests, WAVs, queue and review state are never deleted, overwritten
or regenerated.

## Command line

For a terminal legacy workspace whose already approved WAVs lack a complete
control inventory, first publish an immutable migration successor:

```sh
uv run vntts-pregenerate reviewed-waveform-publication \
  /path/to/base-workspace \
  --workspaces-root /path/to/workspaces
```

The command selects all and only `approved/approved` items. Active workspaces
and empty migrations fail closed. Existing `config_rebase` routes are preserved;
older approved records are labelled `historical_reviewed_waveform` with
`not_reproducible` status and no invented references. Repeating the exact
command returns the same successor with `created=false`.

To authorize Pocket live synthesis for every rejected item that still lacks an
explicit fallback, create the separate rejected-result successor:

```sh
uv run vntts-pregenerate reviewed-rejection-live-fallback \
  /path/to/base-workspace \
  --workspaces-root /path/to/workspaces
```

The command selects all and only `generated/rejected` items without an existing
fallback. It derives the effective character from an active config-rebase route
when present, otherwise from an explicit current voice-manifest binding. A
missing or ambiguous route fails closed. Repeating the exact command is
idempotent and never mutates the base workspace.

```sh
uv run vntts-pregenerate publish-pack \
  --state /path/to/app-data/generated/generation-state.json \
  --queue /path/to/app-data/generation-queue.jsonl \
  --story-index /path/to/app-data/story-index.jsonl \
  --voice-manifest /path/to/app-data/voice-manifest.json \
  --live-sequence-plan /path/to/extractor/live-sequence.json \
  --output /path/to/deliveries/game-v1 \
  --game-id reverse-1999 \
  --game-version 1.0 \
  --producer reverse1999-extractor=0.6.0 \
  --producer visual-novel-text-to-speech=0.1.0
```

`--producer NAME=VERSION` can be repeated. If omitted, the installed VNTTS
package identity is recorded. `--game-id` defaults to the bound state game;
`--game-version` is always explicit. The command prints the final manifest,
optional published live-sequence path, source queue/state hashes and
approved/rejected/live-fallback counts as JSON. When `--live-sequence-plan` is
present, publication copies it into staging, validates its checksum binding
against the exact copied story index and includes it as a core component. The
source plan participates in the final mutation check. Omitting the option ships
no sequence component; publication never guesses ordering or control flow from
story rows.

At runtime the ledger is matched by exact line ID and text SHA-256 after source
audio and approved generated-audio checks. A match permits only the recorded
Pocket provider/model/profile and produces a typed `LiveFallbackRoute`; a
different live backend fails closed. Lines without a ledger entry retain the
ordinary live-routing policy and are not silently treated as an authoring
decision.

The resulting `game-pack.json` can be checked through
`vntts-preflight-game-pack` and consumed directly through the existing Settings,
profile and `VNTTS_GAME_PACK` boundaries described in [game-packs.md](game-packs.md).
