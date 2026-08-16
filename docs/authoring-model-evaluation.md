# Authoring model evaluation

VNTTS owns the generic model-comparison and blind-listening runtime in
`vntts.authoring`. Neither workflow imports an extractor, understands game
chapters or interprets game-specific line IDs.

## Shared corpus and typed rendering

`vntts-benchmark-models` accepts exactly one of:

- a version 1 TTS benchmark corpus with stable sample IDs, voice characters and
  text; or
- a public `vntts.voice-generation-queue` version 1 document, converted to a
  corpus with deterministic round-robin sampling over delivery/emotion labels.

Only queue records with `action=generate` are eligible. Queue IDs, line IDs,
text hashes and voice-character choices are copied as opaque shared fields; no
game arithmetic is reproduced.

Every configured model is evaluated over the same corpus through its typed
`render(SynthesisRequest)` API with `cache_policy=bypass`. The benchmark never
opens an audio output or uses a fake playback sink. Cancelled or limited renders
fail the model report rather than publishing partial audio. Successful reports
use the VNTTS-owned `vntts.voice-model-report` version 1 schema and retain PCM
timing, sample rate, sample count, duration, peak, seed and generation profile.
The aggregate `vntts.voice-model-benchmark` version 1 document records the exact
corpus and per-model report paths; model selection remains a manual decision.

Example model configuration:

```json
[
  {"model_id": "moss/stable", "backend": "moss-tts", "generation_profile": "stable"},
  {"model_id": "moss/expressive", "backend": "moss-tts", "generation_profile": "expressive"}
]
```

Run the multi-model benchmark:

```sh
uv run vntts-benchmark-models \
  --corpus /path/to/corpus.json \
  --models /path/to/models.json \
  --manifest /path/to/voice-manifest.json \
  --output /path/to/model-benchmark
```

Use `--queue /path/to/queue.jsonl --sample-size 24` instead of `--corpus` to
build the shared corpus from a generation queue.

## Blind listening and report semantics

Start a session directly from the aggregate benchmark:

```sh
uv run vntts-listen start \
  --benchmark /path/to/model-benchmark/benchmark.json \
  --output /path/to/listening-session \
  --seed 42
```

Alternatively, start from two or more explicitly selected per-model reports:

```sh
uv run vntts-listen start-reports \
  --reports /path/to/model-a/report.json /path/to/model-b/report.json \
  --output /path/to/listening-session \
  --seed 42
```

The engine NFKC-normalizes text, normalizes the Unicode ellipsis to three ASCII
dots and collapses whitespace before matching the same stable sample across
models. It creates every model pair that shares a sample, shuffles trial order
and A/B orientation deterministically from the seed, then hardlinks or copies
neutral relative WAV aliases. Model identities, source paths and A/B assignment
stay only in mode-0600 `.blind-key.json`; the public session never names a
model.

Runtime commands are:

```sh
uv run vntts-listen status --session /path/to/session.json
uv run vntts-listen next --session /path/to/session.json
uv run vntts-listen score trial-0001 --session /path/to/session.json --preference a
uv run vntts-listen report --session /path/to/session.json
uv run vntts-listen ui --session /path/to/session.json
```

Scoring is append-safe by default: rating an already completed trial requires
explicit `--overwrite`. Reports rank models by preference rate, then wins, then
model ID and include the same sorted pairwise totals as the legacy workflow.
The Qt workbench resumes the first serialized unrated trial, autoplays A then B,
keeps preference controls locked until both sides start, and provides pause,
restart, seek and five-second skip controls.

## Legacy-session compatibility

The runtime dual-reads VNTTS-owned version 1 session/key/report schemas and the
legacy `r1999.model-listening-*` version 1 schemas. Imported sessions use only
their copied relative aliases at runtime; stale absolute source-report and
assignment provenance is not required to resume.

Loading, checking progress or opening a completed imported session does not
rerandomize trials, rewrite the hidden key or regenerate an already equivalent
report. `ensure_listening_report` compares report semantics while ignoring only
the legacy absolute session path and generation timestamp. A read-only gate on
2026-08-16 loaded the existing 45/45-trial, six-model session and its 90 aliases;
all 93 session, hidden-key, report and audio hashes remained unchanged.

The extractor commands remain available as transition shims until extractor
parity is independently verified. This VNTTS ownership block does not delete or
edit extractor code.
