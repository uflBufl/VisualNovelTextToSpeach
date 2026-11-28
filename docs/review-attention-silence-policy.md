# Review-attention silence policy

Generated speech has two separate silence boundaries. The synthesis safety gate
decides whether a WAV may be published at all. The review-attention policy only
decides which already valid WAVs must be included in a cohort's human sample.
An attention flag is never an automatic rejection.

## Current review policy

New cohort plans use policy v3. It flags an internal pause of at least 1.2
seconds for listening and has no overall silence-ratio attention threshold.
Near-clipping can still require attention. Generated speech rate and related
pace metrics are advisory context. A review flag is never a quality verdict.

Published v1 and v2 plans keep their original thresholds and sample membership.
V1 used a ratio of 0.15 and a pause of 0.5 seconds. V2 used 0.30 and 1.0
seconds. A new policy version applies to newly generated review plans.

## Historical calibration evidence

The completed primary Centurion/MOSS/stable review contained 129 WAVs. Its
immutable version-1 plan marked 27 WAVs for technical attention, including 24
for silence or pauses. The operator accepted all three cohorts and reported that
the warned pauses sounded normal. Re-projecting the same state with version 2
leaves four non-silence attention items and zero silence-attention items. The
accepted heard evidence included a `0.2245` silence ratio and a `0.96 s`
internal pause.

The separately accepted Pocket evidence reaches a `0.2576` silence ratio and a
`1.12 s` internal pause. Version 2 therefore removes its ratio false positive
but deliberately retains the longest-pause sample as advisory evidence. This is
the expected conservative boundary: one accepted outlier can still be heard,
while ordinary natural pauses no longer dominate review.

The two unresolved Dobharchu natural-expansion cohorts currently contain 17
WAVs. Their immutable version-1 projection has 14 technical-attention items,
including 12 silence/pause items. A read-only version-2 projection retains five
technical-attention items, all five because of silence or pauses. Previously
rejected Dobharchu evidence contains multi-second internal gaps, so it remains
well above the new advisory boundary.

These measurements are calibration evidence, not new human decisions. The
existing Centurion, Pocket and Dobharchu bundle files and progress documents are
not mutated.

## Synthesis validation

Publication fails when any of these strict limits is exceeded:

- leading silence over `0.8 s`;
- trailing silence over `0.8 s`;
- internal silence over `1.2 s`;
- silence ratio over `0.50`.

Those limits are exercised independently from the review-attention boundary.
Changing them requires new generation-quality evidence and is outside this
calibration.
