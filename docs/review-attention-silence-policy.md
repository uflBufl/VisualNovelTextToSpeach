# Review-attention silence policy

Generated speech has two separate silence boundaries. The synthesis safety gate
decides whether a WAV may be published at all. The review-attention policy only
decides which already valid WAVs must be included in a cohort's human sample.
An attention flag is never an automatic rejection.

## Policy v2

New cohort plans use review-attention policy version 2:

| Advisory flag | Version 1 | Version 2 |
| --- | ---: | ---: |
| `notable silence` | silence ratio at least `0.15` | silence ratio at least `0.30` |
| `notable pause` | longest internal silence at least `0.5 s` | longest internal silence at least `1.0 s` |

The comparison is inclusive. A ratio of exactly `0.30` and a pause of exactly
`1.0 s` are selected. Near-clipping and speech-rate attention rules are
unchanged.

Version 2 is stored explicitly in each new cohort plan together with both
thresholds. Version-1 plans and bundles remain readable and retain their exact
original flags and mandatory samples. The application never silently rebuilds
or shrinks an already published review bundle after a policy update.

## Evidence

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

## Unchanged safety gate

Version 2 does not change synthesis validation. Publication still fails closed
when any of these strict limits is exceeded:

- leading silence over `0.8 s`;
- trailing silence over `0.8 s`;
- internal silence over `1.2 s`;
- silence ratio over `0.50`.

Those limits are exercised independently from the review-attention boundary.
Changing them requires new generation-quality evidence and is outside this
calibration.
