"""Tentative voice suggestions from comparable, checksum-verified previews."""

from collections.abc import Mapping, Sequence

from vntts.pregeneration_voices import VoiceCandidate
from vntts.preview_content_advisor import PreviewContentAdvisor, PreviewContentCheck


def cached_reference_checks(
    candidates: Sequence[VoiceCandidate],
    text: str,
    *,
    backend: str,
    model: str | None,
    language: str | None,
    profile: str,
    advisor: PreviewContentAdvisor | None = None,
) -> dict[str, PreviewContentCheck]:
    """Read exact prior preview evidence; never start inference or download models."""
    if language not in {None, "en", "English"}:
        return {}
    advisor = advisor or PreviewContentAdvisor()
    if not (advisor.root / "content-advice").is_dir():
        return {}
    from vntts.pregeneration_audition import VoiceAuditionError, preview_identity

    checks = {}
    for candidate in candidates:
        try:
            identity = preview_identity(
                candidate,
                text,
                backend=backend,
                model=model,
                language=language,
                profile=profile,
            )
            check = advisor.lookup_saved(identity, text, candidate.source_id)
        except OSError, ValueError, VoiceAuditionError:
            check = None
        if check is not None:
            checks[candidate.source_id] = check
    return checks


def recommended_reference(
    candidates: Sequence[VoiceCandidate],
    checks: Mapping[str, PreviewContentCheck],
    *,
    saved_source_id: str | None = None,
) -> str | None:
    """Suggest an alternative only with stronger comparable content evidence.

    An ASR match is not a quality score. Unknown and uncertain checks retain the
    baseline, and content evidence cannot outweigh character identity evidence.
    """
    if not candidates:
        return None
    if saved_source_id is not None:
        return next(
            (c.source_id for c in candidates if c.source_id == saved_source_id),
            candidates[0].source_id,
        )
    baseline = candidates[0]
    original = checks.get(baseline.source_id)
    if original is None or original.advice.status != "review_possible_content_mismatch":
        return baseline.source_id
    for candidate in candidates[1:]:
        alternative = checks.get(candidate.source_id)
        if (
            candidate.match_score >= baseline.match_score
            and alternative is not None
            and alternative.source_id == candidate.source_id
            and original.source_id == baseline.source_id
            and alternative.text == original.text
            and alternative.seed == original.seed
            and alternative.advice.status == "no_asr_mismatch"
        ):
            return candidate.source_id
    return baseline.source_id
