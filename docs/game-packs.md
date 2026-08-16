# Game-pack import boundary

VNTTS consumes the released `vntts.game-pack` version 1 contract from
`vntts-artifacts` v0.6.1. A pack manifest binds a required versioned story
index and voice manifest, every referenced voice WAV, an optional generated
audio manifest and every generated WAV, the producing tool versions, and a
SHA-256 digest for each file.

`vntts.game_pack.import_game_pack(path)` is the public preflight entry point.
It delegates schema, safe-path, component and checksum validation to the shared
contract and returns a `GamePackImport` containing absolute paths for the three
VNTTS inputs. `GamePackImport.apply_to(settings)` and
`apply_game_pack(settings, path)` return a new immutable `AppSettings`; neither
copies, deletes nor rewrites pack or application data. A pack without a
generated-audio component explicitly clears a stale generated-audio setting.

The **Game pack** setting and `VNTTS_GAME_PACK` environment variable run the
same preflight when settings are loaded. The checksum-bound pack components
take precedence over separately saved story-index, voice-manifest and
generated-audio paths. Game profiles persist the pack manifest path and repeat
preflight before a profile is activated. Invalid or modified packs are rejected
instead of silently falling back to stale component paths.

To validate a delivery without starting the desktop application:

```sh
uv run vntts-preflight-game-pack /path/to/game-pack.json
```

The command prints the validated game identity and resolved input paths as
JSON. It is read-only.

Producer/consumer compatibility is covered by a synthetic contract test. It
uses the public shared writers to publish a story index, voice manifest,
generated-audio manifest, referenced PCM WAVs and a complete pack, then imports
that pack through the public VNTTS boundary and loads all three inputs through
the runtime chapter, voice and generated-audio consumers. The test also checks
that tampering with a referenced WAV fails preflight and that unrelated
application data remains untouched.
