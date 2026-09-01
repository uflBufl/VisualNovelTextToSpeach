# Speaker identity diagnostics

Speaker embeddings may reduce repeated voice choices only after a labelled,
held-out evaluation. They never grant synthesis authority and never modify a
voice manifest, source-reference decision, workspace, review or game pack.

## Pinned runtime

The optional `speaker-identity` dependency installs SpeechBrain 1.0.3. The
diagnostic model is
[`speechbrain/spkrec-ecapa-voxceleb`](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)
at immutable revision
`0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`. Both the model snapshot and
[SpeechBrain 1.0.3](https://github.com/speechbrain/speechbrain/tree/v1.0.3)
declare Apache-2.0. The managed installer downloads only these files and checks
each SHA-256 before publication:

| File | SHA-256 |
| --- | --- |
| `classifier.ckpt` | `fd9e3634fe68bd0a427c95e354c0c677374f62b3f434e45b78599950d860d535` |
| `embedding_model.ckpt` | `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2` |
| `hyperparams.yaml` | `6f78854fa04ba59e761437b76a2575d3aba5e5016de3e9b69f0c9a5077fb1a41` |
| `label_encoder.txt` | `e13c3a167bb4112685670ee896d20e2b565af16b3a4ceeaa8689fa4d22adb8b9` |
| `mean_var_norm_emb.ckpt` | `cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33` |

The initial implementation is deliberately CPU-only. A non-CPU request fails
before loading the model. The weights stay in the device-local authoring model
directory; they are not bundled into game packs.

Install the optional implementation and pinned snapshot:

```bash
uv sync --extra speaker-identity
uv run vntts-pregenerate speaker-identity-model-install
uv run vntts-pregenerate speaker-identity-model-status
```

`speaker-identity-model-install --source DIRECTORY` imports the same five files
without network access and applies the identical checksum gate.

## Evaluation flow

First publish the exact current reference inventory:

```bash
uv run vntts-pregenerate speaker-identity-inventory VOICES.json \
  --output speaker-reference-inventory.json
```

Every entry binds character, speaker, manifest-relative path, audio SHA-256,
sample rate, channels, frames and duration. Reloading the inventory rebuilds it
from the manifest and fails if the manifest or any reference changed.

Create a draft JSON list or an object containing `pairs`. Each pair contains
`left_reference_id`, `right_reference_id`, `partition` and `relationship`:

```json
{
  "pairs": [
    {
      "left_reference_id": "REFERENCE_ID_1",
      "right_reference_id": "REFERENCE_ID_2",
      "partition": "fit",
      "relationship": "same-speaker"
    },
    {
      "left_reference_id": "REFERENCE_ID_3",
      "right_reference_id": "REFERENCE_ID_4",
      "partition": "held-out",
      "relationship": "same-character/different-age"
    }
  ]
}
```

Allowed relationships are `same-speaker`, `different-speaker` and
`same-character/different-age`. The last two are hard negative boundaries. An
unordered reference pair may occur only once, and a reference may belong to only
one partition, so neither a pair nor an audio recording can leak from fit into
held-out evidence.

Publish the labels and run the diagnostic:

```bash
uv run vntts-pregenerate speaker-identity-labels \
  speaker-reference-inventory.json pair-draft.json \
  --output speaker-identity-labels.json

uv run vntts-pregenerate speaker-identity-evaluate \
  speaker-reference-inventory.json speaker-identity-labels.json \
  --offline --output speaker-identity-report.json
```

The fit threshold exists only when every labelled fit positive has a smaller
cosine distance than every labelled fit negative. The report includes held-out
confusion and the exact violated boundaries. `threshold_eligible` can be true
only when held-out data contains both positive and negative pairs and has zero
false merges. Even an eligible report remains diagnostic until its exact corpus
and policy are explicitly approved for a downstream consumer.

## Verified implementation baseline

The repository's current 408-reference manifest produced inventory ID
`45dfd6f13ca85ecfafdae123f9c709524486f7da4e7ee98f7b55f5b1cb581229`.
A local offline smoke test loaded the pinned five-file snapshot, produced finite
192-dimensional embeddings and measured cosine distance `0.306610107421875`
between `37-01.ogg` and `37-02.ogg`. This proves installation and inference, not
a production threshold.
