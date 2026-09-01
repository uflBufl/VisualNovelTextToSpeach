"""Shared user-facing exception boundary for the authoring CLI."""

from __future__ import annotations

from vntts_artifacts import StoryIndexError, VoiceGenerationQueueError
from vntts_artifacts.voice_manifest import VoiceManifestError

from vntts.authoring.bulk_generation import BulkGenerationError
from vntts.authoring.cli_audio_events import (
    AudioEventCompositionError,
    AudioEventReviewError,
)
from vntts.authoring.cli_cohort_reviews import CohortReviewError
from vntts.authoring.cli_render_reviews import (
    ReferenceRenderComparisonError,
    RenderHypothesisReviewError,
)
from vntts.authoring.cli_silence_comparison import SilenceComparisonError
from vntts.authoring.cli_speaker_identity import (
    SpeakerIdentityError,
    SpeakerIdentityModelError,
)
from vntts.authoring.cli_speech_robustness import SpeechRobustnessCommandError
from vntts.authoring.cli_terminal_conflicts import (
    TerminalConflictResolutionError,
    TerminalConflictReviewError,
    TerminalConflictSuccessorError,
)
from vntts.authoring.cli_voice_quality import (
    VoiceQualityGateError,
    VoiceRepairComparisonError,
)
from vntts.authoring.delivery import DeliveryAnnotationError
from vntts.authoring.experimental_composite_voice import ExperimentalCompositeVoiceError
from vntts.authoring.failed_control_carry import FailedControlCarryError
from vntts.authoring.failed_prompt_hypothesis import FailedPromptHypothesisError
from vntts.authoring.failure_reference_audit import FailureReferenceAuditError
from vntts.authoring.failure_reference_binding import FailureReferenceBindingError
from vntts.authoring.failure_regeneration import FailureRegenerationError
from vntts.authoring.game_pack import FinalGamePackError
from vntts.authoring.known_role_reuse import KnownRoleReuseError
from vntts.authoring.legacy_import import LegacyAuthoringImportError
from vntts.authoring.listening_import import ListeningImportError
from vntts.authoring.missing_voice_live_fallback import MissingVoiceLiveFallbackError
from vntts.authoring.missing_voice_reuse import MissingVoiceReuseError
from vntts.authoring.missing_voice_reuse_binding import MissingVoiceReuseBindingError
from vntts.authoring.missing_voice_reuse_review import MissingVoiceReuseReviewError
from vntts.authoring.portrait_aliases import PortraitAliasError
from vntts.authoring.queue_builder import GenerationQueueBuildError
from vntts.authoring.queue_extension import QueueExtensionError
from vntts.authoring.reference_selection import ReferenceSelectionError
from vntts.authoring.source_reference_bindings import SourceReferenceBindingError
from vntts.authoring.source_reference_quality import SourceReferenceQualityError
from vntts.authoring.source_reference_review import SourceReferenceReviewError
from vntts.authoring.workbench import AuthoringWorkbenchError

USER_ERRORS = (
    AudioEventReviewError,
    AudioEventCompositionError,
    AuthoringWorkbenchError,
    GenerationQueueBuildError,
    BulkGenerationError,
    CohortReviewError,
    DeliveryAnnotationError,
    ExperimentalCompositeVoiceError,
    FinalGamePackError,
    FailureRegenerationError,
    FailureReferenceAuditError,
    FailureReferenceBindingError,
    FailedControlCarryError,
    FailedPromptHypothesisError,
    LegacyAuthoringImportError,
    ListeningImportError,
    MissingVoiceReuseError,
    MissingVoiceReuseBindingError,
    MissingVoiceReuseReviewError,
    MissingVoiceLiveFallbackError,
    KnownRoleReuseError,
    PortraitAliasError,
    QueueExtensionError,
    ReferenceSelectionError,
    ReferenceRenderComparisonError,
    RenderHypothesisReviewError,
    SilenceComparisonError,
    SpeakerIdentityError,
    SpeakerIdentityModelError,
    SpeechRobustnessCommandError,
    SourceReferenceReviewError,
    SourceReferenceQualityError,
    SourceReferenceBindingError,
    StoryIndexError,
    TerminalConflictResolutionError,
    TerminalConflictReviewError,
    TerminalConflictSuccessorError,
    VoiceGenerationQueueError,
    VoiceManifestError,
    VoiceQualityGateError,
    VoiceRepairComparisonError,
)
