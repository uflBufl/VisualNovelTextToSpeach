"""Bounded preview content advice before a story voice plan is published."""

from collections.abc import Callable
from typing import Protocol

from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionCancelled,
    VoiceAuditionPreview,
)
from vntts.pregeneration_voices import (
    Cancellation,
    PregenerationVoiceCancelled,
    VoiceCandidate,
    VoiceGroup,
    VoicePlan,
)
from vntts.preview_content_advisor import (
    PreviewContentAdvisor,
    PreviewContentCheck,
    PreviewContentCheckCancelled,
)
from vntts.reference_recommendation import (
    cached_reference_checks,
    recommended_reference,
)


class ReferencePreviewer(Protocol):
    def close(self) -> None: ...

    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: Cancellation | None = None,
        progress: Callable[[str], object] | None = None,
    ) -> VoiceAuditionPreview: ...


def _check_cancelled(cancellation: Cancellation | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise PregenerationVoiceCancelled("Reference qualification was cancelled")


def qualify_story_references(
    plan: VoicePlan,
    *,
    preview_service: ReferencePreviewer,
    content_advisor: PreviewContentAdvisor,
    cancellation: Cancellation | None = None,
    progress: Callable[[str], object] | None = None,
) -> dict[str, str]:
    """Keep character identity authoritative and checker failures unknown."""
    notify = progress or (lambda _message: None)
    selections: dict[str, str] = {}
    for group in plan.groups:
        _check_cancelled(cancellation)
        if not _automatic_group(group):
            continue
        candidates = group.candidates[:3]
        baseline = candidates[0].source_id
        selections[group.group_id] = baseline
        if len(candidates) < 2 or plan.synthesis_language not in {
            None,
            "en",
            "English",
        }:
            continue
        notify(f"Checking automatic references for {group.character}")
        checks = cached_reference_checks(
            candidates,
            COMPARISON_PREVIEW_TEXT,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
            advisor=content_advisor,
        )
        previews = _prepare_previews(
            plan, group, candidates, checks, preview_service, cancellation, notify
        )
        if previews:
            _check_cancelled(cancellation)
            notify(f"Checking preview speech for {group.character}")
            try:
                checks.update(
                    (check.source_id, check)
                    for check in content_advisor.check(
                        tuple(previews), install_model=True, cancel_event=cancellation
                    )
                )
            except PreviewContentCheckCancelled as error:
                raise PregenerationVoiceCancelled(str(error)) from error
            except Exception as error:
                _check_cancelled(cancellation)
                notify(
                    f"Reference content advice unavailable for {group.character}: {error}"
                )
                # A later failed check must not discard earlier exact cached evidence.
                checks.update(
                    cached_reference_checks(
                        candidates,
                        COMPARISON_PREVIEW_TEXT,
                        backend=plan.synthesis_backend,
                        model=plan.synthesis_model,
                        language=plan.synthesis_language,
                        profile=plan.synthesis_profile,
                        advisor=content_advisor,
                    )
                )
        _check_cancelled(cancellation)
        selections[group.group_id] = (
            recommended_reference(candidates, checks) or baseline
        )
    return selections


def _automatic_group(group: VoiceGroup) -> bool:
    return bool(
        group.candidates
        and not group.resolution.startswith("saved-")
        and group.route in {"voice", "needs-audition"}
        and (group.route == "voice" or group.candidates[0].match_score >= 90)
    )


def _prepare_previews(
    plan: VoicePlan,
    group: VoiceGroup,
    candidates: tuple[VoiceCandidate, ...],
    checks: dict[str, PreviewContentCheck],
    preview_service: ReferencePreviewer,
    cancellation: Cancellation | None,
    notify: Callable[[str], object],
) -> tuple[VoiceAuditionPreview, ...]:
    previews: list[VoiceAuditionPreview] = []
    for index, candidate in enumerate(candidates, start=1):
        _check_cancelled(cancellation)
        if candidate.source_id in checks:
            continue
        notify(f"Preparing {group.character} reference {index}/{len(candidates)}")
        try:
            previews.append(
                preview_service.generate(
                    plan,
                    group,
                    candidate.source_id,
                    text=COMPARISON_PREVIEW_TEXT,
                    cancel_event=cancellation,
                    progress=notify,
                )
            )
        except VoiceAuditionCancelled as error:
            raise PregenerationVoiceCancelled(str(error)) from error
        except Exception as error:
            _check_cancelled(cancellation)
            notify(f"Reference check unavailable for {group.character}: {error}")
    return tuple(previews)
