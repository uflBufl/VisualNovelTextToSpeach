# Immutable voice-reference comparison and selection

Voice-reference order changes synthesis and therefore belongs to immutable
authoring configuration. Do not edit an active workspace manifest in place.
Inspect a character's current candidates first:

```bash
uv run vntts-pregenerate reference-report \
  --voice-manifest VOICES/manifest.json \
  --character Rhiannon > rhiannon-reference-report.json
```

The report reads each WAV once and binds duration, peak, RMS, clipping, silence,
DC offset and PCM SHA-256 to that exact byte snapshot. Objective ranking may
reject a technically unusable file, but it does not decide speaker similarity,
music/background contamination, spoken content or pronunciation. Those remain
manual listening decisions.

After listening, publish a new manifest without overwriting the source:

```bash
uv run vntts-pregenerate select-reference \
  --voice-manifest VOICES/manifest.json \
  --character Rhiannon \
  --reference-number 2 \
  --output VOICES/rhiannon-reference-2.json
```

The selected candidate becomes the first reference; the remaining candidates
keep their relative order. The output contains a versioned
`vntts.authoring.reference_selection` extension with the source-manifest hash,
selected reference/hash and complete candidate inventory. Publication is
atomic and no-overwrite. Absolute, escaping, missing, duplicate or symlinked
references are rejected, and all source bytes are rechecked immediately before
publication.

Pass the new manifest to `vntts-pregenerate create-workspace`. Workspace
creation copies every reference, then validates the selection extension against
the copied bytes. A changed candidate, reordered first reference, forged digest
or incomplete inventory fails closed. The selected manifest and copied WAVs
become part of the config-addressed workspace and synthesis-control digest.
Existing terminal decisions for the affected character can be carried forward
only when their original control hashes still match; otherwise regenerated WAVs
require ordinary manual review.

Use `failure-repair-plan` and `failure-report` to choose the exact failed cohort
that justifies a comparison. The manifest selection itself is character-wide,
so generate only explicit queue IDs until the candidate proves acceptable.
