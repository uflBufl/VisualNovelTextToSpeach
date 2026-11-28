# Game-pack import boundary

A `vntts.game-pack` v1 or v2 manifest binds a story index, a voice manifest,
their reference WAVs and optional generated audio or a live sequence plan. The
shared `vntts-artifacts` contract validates component identities, contained
paths and file checksums. VNTTS also validates any bound source-audio semantic
evidence, including its checksum, evidence ID and correspondence to the story
records.

Choose **Game pack** in Settings or a game profile, or set `VNTTS_GAME_PACK`.
The pack components take precedence over separately configured component paths.
An invalid or modified pack fails preflight. Applying a pack without generated
audio clears the previous generated-audio setting. A pack without a sequence
clears the previous sequence by default. The explicit settings path that
preserves an external sequence must request that behavior separately.

To validate a pack from a source checkout:

```sh
uv run vntts-preflight-game-pack /path/to/game-pack.json
```

The command prints the game identity and resolved inputs as JSON. The Python
API `vntts.game_pack.import_game_pack(path)` returns a validated
`GamePackImport`. Its `apply_to(settings)` method and the `apply_game_pack`
helper return settings for the pack without modifying it.

The pack establishes available inputs for [live audio routing](live-audio-routing.md).
Routing owns matching, audio priority and the completion rules. See
[Game-pack publication](authoring-game-pack-publication.md) for the producer
workflow. The [producer/consumer tests](../tests/test_game_pack.py) exercise
round-trip import and tampered component rejection.
