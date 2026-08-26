# Collection-driven generation queues

VNTTS owns the game-independent boundary that turns a validated story index and
voice manifest into a `vntts.voice-generation-queue` v1 document. It requires
the lossless public `StoryIndexDocument` and `StoryIndexRecord` APIs introduced
in `vntts-artifacts` v0.6.1 and available through the pinned v0.6.2 release.
The builder never parses raw
producer JSON, imports an extractor, interprets a game-specific line ID, or
performs chapter arithmetic.

## Public API

`plan_generation_queue(document, voice_entries, voice_manifest_path, ...)`
accepts a public typed story document, validated `VoiceManifestEntry` values
and their exact source path. Collection selection
uses declared `collection_id` values and preserves story-index order. Unknown
producer fields such as emotion, delivery and prompt adapters are copied as
queue extensions, while canonical queue identity and policy fields cannot be
overridden by an extension. The plan binds canonical digests of the complete
typed story document and voice entries, so it remains publishable even when a
caller did not inspect raw JSON. Both `StoryIndexDocument.path` and the explicit
voice-manifest path must identify readable source files; their exact SHA-256
digests are always bound into publishable queue metadata.

`inspect_generation_queue(story_index, voice_manifest, ...)` loads both inputs
through the shared readers and binds their exact SHA-256 digests into the queue
metadata. It returns a plan without writing anything. `publish_generation_queue`
validates that plan through the shared queue writer and publishes atomically.

Delivery annotations default to a lossless `preserve` overlay, so existing
story-owned emotion, delivery, prompt adapters and unknown extensions retain
their exact queue representation. The optional
`legacy-english-heuristic-v1` policy fills only wholly unannotated records and
records per-item provenance in queue-owned metadata without affecting queue IDs
or text hashes. Partial source annotations are reported but never completed or
overwritten. See the
[delivery-annotation contract](authoring-delivery-annotations.md).

Voice-character aliases are resolved to the manifest's canonical character.
Every reference must be a POSIX-relative path whose resolved target remains
inside the manifest directory; absolute paths, parent traversal, backslashes
and symlink escapes fail preflight before queue publication.
Preflight treats a generation item as ready only when every configured
reference for its resolved character exists locally and probes as PCM16 mono
WAV. This conservative rule is
valid for cloning backends that consume all references as well as backends that
use the first one. Pathless typed planning is rejected because it cannot bind
or verify the exact manifest. Missing characters, empty reference lists,
references that do not exist and non-decodable references are
reported as missing-reference work;
they remain in the queue so authoring can repair them without rebuilding line
identity.

## Source-audio policy

Only the canonical typed `source_audio_status` controls the queue action:

| Source status | Queue result |
| --- | --- |
| `available` | excluded; use the existing source audio |
| `absent` | `generate` |
| `unavailable` | `prefer_source_audio` |
| `unknown` | requires explicit `resolve_audio` or `manual_review` |

There is deliberately no default for `unknown`: legacy unchecked and unresolved
records normalize to the same canonical status but require different authoring
decisions. Non-speakable records are also excluded. The preflight summary
separates ready generation, missing references, recoverable source audio,
manual review, available audio, non-speakable records and records outside the
selected collections. The broader workbench still needs a dedicated
sound-effect category before its separate preflight TODO is complete.

## Command line

Preflight one or more declared collections without creating output:

```sh
uv run vntts-pregenerate preflight-queue \
  --story-index /path/to/story-index.jsonl \
  --voice-manifest /path/to/voice-manifest.json \
  --delivery-policy preserve \
  --collection main-story \
  --unknown-action resolve_audio
```

Publish the exact validated plan:

```sh
uv run vntts-pregenerate build-queue \
  --story-index /path/to/story-index.jsonl \
  --voice-manifest /path/to/voice-manifest.json \
  --collection main-story \
  --unknown-action resolve_audio \
  --output /path/to/generation-queue.jsonl
```

Omit `--collection` to include all story-index records. If any selected record
has `unknown` source status, omitting `--unknown-action` fails before output is
created. Queue IDs remain `line_id:text_sha256[:16]`, with the hash calculated
from exact UTF-8 text by the shared story-index validator.
