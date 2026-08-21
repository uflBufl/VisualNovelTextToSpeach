# Reusable voice-quality gates

A cohort acceptance proves that a human heard a checksum-bound sample under one
exact synthesis control. It does not justify carrying approved WAVs into an
unrelated story. A reusable voice-quality gate preserves the smaller conclusion:
the exact voice controls have an accepted baseline and may enter a new story's
normal risk-based sample.

Publish a gate only from the immutable accepted plan and decision evidence:

```console
uv run vntts-pregenerate voice-quality-gate WORKSPACE PLAN.json DECISION.json \
  --output voice-quality-gate.json
```

The no-replace document binds the accepted decision and reviewed WAV evidence,
voice character and speaker, ordered reference SHA-256 values, backend, model,
generation profile, current model file or directory digest, prompt policy, text
transform, repair strategy and applied source-reference identity. Story,
workspace, queue ID and random seed remain source evidence but are deliberately
excluded from the reuse identity.

Check one later pending item without changing its state:

```console
uv run vntts-pregenerate voice-quality-check voice-quality-gate.json \
  LATER_WORKSPACE QUEUE_ID
```

`control_match_story_sample_required` means the controls match. Every
technical-attention WAV and the deterministic clean sample selected by the
ordinary cohort planner must still be heard in the later story. The gate never
approves or rejects a WAV. `new_review` lists changed identity fields. Reference
order or bytes, model bytes, backend/model/profile, voice/age/portrait variant,
prompt/transform and repair strategy all invalidate reuse.
