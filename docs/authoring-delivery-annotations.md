# Delivery-annotation authoring

`vntts.authoring.delivery` owns the game-independent delivery annotation policy
used when an authoring source has no delivery metadata. The policy is explicit
and non-destructive: queue planning preserves source-owned `annotation_version`,
`emotion`, `delivery`, `prompt_adapters` and every unrelated producer extension
exactly. It never rewrites the story index.

## Public API and compatibility

`annotate_delivery(text, ...)` preserves the extractor's deterministic version
1 behavior for canonical English authoring inputs. The port intentionally keeps:

- the ASCII word pattern and exact English emotion lexicons;
- lexicographic emotion tie-breaking;
- punctuation, uppercase, stage-direction and context cue ordering;
- exact prompt text and Chatterbox/CosyVoice/Fish Speech adapter values;
- `kind == "narration"` as the only special zero-score default.

Every other kind, including unknown values and speakable sound-effect records,
uses the legacy dialogue-neutral default. Non-speakable records are excluded by
queue planning before annotation. The later bulk-generation filter still skips
legacy pure `*sound effect*` text. The policy name
`legacy-english-heuristic-v1` makes both the language and compatibility scope
explicit; this is not a multilingual emotion classifier.

`apply_delivery_policy(record, policy=...)` returns a
`DeliveryPolicyApplication` without mutating the input mapping. Its `origin` is
one of `policy`, `source_complete`, `source_partial`, or `none`. A complete
source annotation requires exact non-boolean `annotation_version == 1` plus
dictionary-valued emotion, delivery and prompt adapters. Any partial, null,
unversioned or differently versioned source annotation is preserved unchanged
and reported as partial; policy output is never mixed into it.

## Queue overlay and provenance

The collection-driven queue builder defaults to `preserve`. Explicit preserve
and the omitted option produce identical metadata and item records for the same
timestamp. Unknown producer extensions remain lossless, including a source
field named `vntts.authoring.delivery`. Canonical `queue_id`, `line_id`, exact
text and `text_sha256` never depend on annotations.

Opting into `legacy-english-heuristic-v1` annotates only records with none of
the four annotation fields. Generated emotion/delivery/prompt fields retain the
legacy wire shape. Their origin is recorded outside item extensions in queue
metadata under `delivery_annotation_policy`: policy name/version, missing-only
mode, complete/partial/source counts, and one input SHA-256 record per generated
queue ID. Keeping provenance in queue-owned metadata prevents a source extension
from spoofing or colliding with policy ownership.

These prompt adapters are authoring and benchmark metadata today. Typed
`SynthesisRequest` does not yet carry them, so bulk generation records
`prompt_applied=false` and does not claim that annotations changed a waveform.

## Command line

Inspect one policy result without writing data:

```sh
uv run vntts-pregenerate annotate-delivery \
  --text 'Help! Run from the monster!' \
  --speaker 'Test Hero' \
  --kind dialogue
```

Opt into the policy while preflighting or publishing a collection queue:

```sh
uv run vntts-pregenerate build-queue \
  --story-index /path/to/story-index.jsonl \
  --voice-manifest /path/to/voice-manifest.json \
  --delivery-policy legacy-english-heuristic-v1 \
  --output /path/to/generation-queue.jsonl
```

Omit the flag, or pass `--delivery-policy preserve`, to copy only source-owned
annotations. Queue preflight and build use the same overlay and queue metadata.
