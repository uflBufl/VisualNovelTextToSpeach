# Final game-pack publication

`vntts.authoring.publish_final_game_pack` is the final, non-destructive
authoring boundary. It creates a portable `vntts.game-pack` version 1 directory
from a fully reviewed bulk-generation state plus the exact queue, story index
and voice manifest that produced it. The implementation uses only the released
vntts-artifacts v0.6.1 game-pack and binding APIs; lossless generated-audio
extensions remain authoritative in generation state.

## Publication gates

Final publication requires all of the following:

- state is bound to the SHA-256 of the exact raw queue bytes;
- state contains exactly the selected queue IDs, with every item terminal as
  approved or rejected; active, failed, pending or partial state is rejected;
- queue metadata contains non-optional source paths and SHA-256 bindings for
  its original story index and voice manifest. Story identity must still match
  exactly. A deliberate replacement voice snapshot is allowed only when every
  terminal state item proves that exact selected manifest and references in its
  synthesis-control inventory;
- every approved line ID/text hash exists in the bound story index;
- current-schema state contains the per-control inventory captured by bulk
  synthesis. The manifest and every referenced voice WAV must retain the same
  path and bytes used during synthesis. Legacy or older unbound states must be
  migrated or regenerated before final publication;
- every generated WAV still matches authoritative state hashes, PCM quality and
  its approved review decision.

The original generated-audio manifest may be absent or stale. It is not the
review authority and is never rewritten by this command. A fresh approved-only
manifest is built inside staging from the exact state snapshot. Rejected audio,
partial WAVs, queue/state files and review diagnostics remain in application
data and are not shipped.

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
publication lease ownership. The staging directory is then renamed with an
atomic no-replace primitive. macOS uses `renamex_np(RENAME_EXCL)`, Linux uses
`renameat2(RENAME_NOREPLACE)`, and Windows uses its non-replacing rename
semantics. Publication refuses an existing file, symlink, empty directory or
populated directory; it never merges with or overwrites a previous delivery.
Unsupported filesystems/runtimes fail closed.

Once the no-replace rename succeeds, publication is committed. A later lease
cleanup ambiguity cannot turn that committed result into a reported failure.
Source manifests, WAVs, queue and review state are never deleted, overwritten
or regenerated.

## Command line

```sh
uv run vntts-pregenerate publish-pack \
  --state /path/to/app-data/generated/generation-state.json \
  --queue /path/to/app-data/generation-queue.jsonl \
  --story-index /path/to/app-data/story-index.jsonl \
  --voice-manifest /path/to/app-data/voice-manifest.json \
  --output /path/to/deliveries/game-v1 \
  --game-id reverse-1999 \
  --game-version 1.0 \
  --producer reverse1999-extractor=0.6.0 \
  --producer visual-novel-text-to-speech=0.1.0
```

`--producer NAME=VERSION` can be repeated. If omitted, the installed VNTTS
package identity is recorded. `--game-id` defaults to the bound state game;
`--game-version` is always explicit. The command prints the final manifest,
source queue/state hashes and approved/rejected counts as JSON.

The resulting `game-pack.json` can be checked through
`vntts-preflight-game-pack` and consumed directly through the existing Settings,
profile and `VNTTS_GAME_PACK` boundaries described in [game-packs.md](game-packs.md).
