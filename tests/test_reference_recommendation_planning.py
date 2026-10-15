import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.pregeneration_fixtures import clean_wav_bytes
from tests.story_fixtures import write_content
from tests.voice_manifest_fixtures import write_player_candidate_manifest
from vntts.document_identity import sha256_file
from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionPreview,
    preview_identity,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoiceDecisionStore, VoicePlanStore
from vntts.preview_content_advisor import PreviewContentAdvisor
from vntts.settings import AppSettings
from vntts.story_reference_qualification import qualify_story_references
from vntts.voice_library import VoiceLibrary
from vntts.voices import (
    CharacterVoiceRegistry,
    remember_voice_binding,
)


class ReferenceRecommendationPlanningTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        content = inspect_story_index(write_content(self.root / "content"))
        self.jobs = PregenerationJobStore(self.root / "jobs")
        self.job = self.jobs.create_or_resume(content, ("story",))
        self.manifest = write_player_candidate_manifest(
            self.root / "voices", self.job.story_index_sha256, quality_scores=(100, 100)
        )
        self.settings = AppSettings(
            speech_backend="coqui-xtts", tts_language="en", xtts_terms_accepted=True
        )
        self.transcribe = Mock()
        self.advisor = PreviewContentAdvisor(
            self.root / "previews",
            transcriber_factory=lambda _: self.transcribe,
            model_resolver=lambda: self.root / "model",
        )
        self.advisor.root.mkdir()
        patcher = patch(
            "vntts.reference_recommendation.PreviewContentAdvisor",
            return_value=self.advisor,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.initial_plan = self._plan()
        self.initial = self._group(self.initial_plan)
        self.baseline, self.alternative = self.initial.candidates
        self.assertEqual(self.initial.route, "voice")
        self.assertEqual(self.initial.source_id, self.baseline.source_id)
        self.assertEqual(self.baseline.match_score, self.alternative.match_score)

    def _plan(self, *, settings=None, library=None, decisions=None, qualifier=None):
        if qualifier is None:
            previewer = Mock()
            previewer.generate.side_effect = RuntimeError("No uncached test renderer")

            def qualifier(plan):
                return qualify_story_references(
                    plan, preview_service=previewer, content_advisor=self.advisor
                )

        return VoicePlanStore(
            self.jobs, voice_library=library, decisions=decisions
        ).create(
            self.job,
            settings or self.settings,
            manifest_path=self.manifest,
            reference_qualifier=qualifier,
        )

    @staticmethod
    def _group(plan):
        return next(group for group in plan.groups if group.character == "Rhiannon")

    def _cache_checks(self, *, text=COMPARISON_PREVIEW_TEXT, seeds=(0, 0)):
        previews = []
        plan = self.initial_plan
        for candidate, seed in zip(self.initial.candidates, seeds, strict=True):
            identity = preview_identity(
                candidate,
                text,
                backend=plan.synthesis_backend,
                model=plan.synthesis_model,
                language=plan.synthesis_language,
                profile=plan.synthesis_profile,
            )
            path = self.advisor.root / f"{identity}.wav"
            path.write_bytes(clean_wav_bytes())
            checksum = sha256_file(path)
            path.with_suffix(".json").write_text(
                json.dumps(
                    {"identity": identity, "audio_sha256": checksum, "seed": seed}
                )
            )
            previews.append(
                VoiceAuditionPreview(
                    identity=identity,
                    group_id=self.initial.group_id,
                    candidate_source_id=candidate.source_id,
                    text=text,
                    backend=plan.synthesis_backend,
                    model=plan.synthesis_model,
                    generation_profile=plan.synthesis_profile,
                    seed=seed,
                    path=path,
                    audio_sha256=checksum,
                    sample_rate=24000,
                    duration_seconds=1,
                    reused=True,
                )
            )
        self.transcribe.side_effect = [text + " Wine.", text]
        checks = self.advisor.check(tuple(previews))
        self.assertEqual(checks[0].advice.status, "review_possible_content_mismatch")
        self.assertEqual(checks[1].advice.status, "no_asr_mismatch")
        return previews

    def test_unbound_automatic_default_uses_cached_comparable_preview_evidence(self):
        self._cache_checks()
        library = VoiceLibrary(self.root / "library")
        preview_service = Mock()
        selected = self._group(
            self._plan(
                library=library,
                qualifier=lambda plan: qualify_story_references(
                    plan, preview_service=preview_service, content_advisor=self.advisor
                ),
            )
        )
        preview_service.generate.assert_not_called()
        self.assertEqual(selected.route, "voice")
        self.assertEqual(selected.source_id, self.alternative.source_id)
        self.assertEqual(selected.resolution, "automatic-content-suggestion")
        self.assertEqual(
            library.binding("Rhiannon").source_sha256s,
            self.alternative.reference_sha256s,
        )
        self.assertEqual(self.transcribe.call_count, 2)

    def test_existing_manual_or_automatic_binding_retains_player_voice(self):
        self._cache_checks()
        registry = CharacterVoiceRegistry.from_file(self.manifest)
        for method in ("manual", "automatic"):
            with self.subTest(method=method):
                library = VoiceLibrary(self.root / method)
                remember_voice_binding(
                    library,
                    registry,
                    "Rhiannon",
                    self.baseline.source_id,
                    method=method,
                )
                binding = library.binding("Rhiannon")
                selected = self._group(self._plan(library=library))
                self.assertEqual(
                    selected.reference_sha256s, self.baseline.reference_sha256s
                )
                self.assertEqual(selected.resolution, "saved-voice-assignment")
                self.assertEqual(library.binding("Rhiannon"), binding)
        self.assertEqual(self.transcribe.call_count, 2)

    def test_explicit_saved_decision_precedes_cached_recommendation(self):
        decisions = VoiceDecisionStore(self.root / "decisions.json")
        decisions.remember(self.initial, self.baseline.source_id)
        self._cache_checks()
        selected = self._group(self._plan(decisions=decisions))
        self.assertEqual(selected.source_id, self.baseline.source_id)
        self.assertEqual(selected.resolution, "saved-player-decision")
        self.assertEqual(self.transcribe.call_count, 2)

    def test_different_preview_phrase_cannot_change_automatic_default(self):
        self._cache_checks(text=self.initial.sample_text)
        selected = self._group(self._plan())
        self.assertEqual(selected.source_id, self.baseline.source_id)
        self.assertEqual(self.transcribe.call_count, 2)

    def test_changed_backend_or_profile_invalidates_cached_recommendation(self):
        self._cache_checks()
        changed_settings = (
            self.settings.updated(
                speech_backend="pocket-tts",
                offline_speech_backend="pocket-tts",
                pocket_gated_model_accepted=True,
            ),
            self.settings.updated(tts_profile="natural", offline_tts_profile="natural"),
        )
        for settings in changed_settings:
            with self.subTest(settings=settings):
                selected = self._group(self._plan(settings=settings))
                self.assertEqual(selected.source_id, self.baseline.source_id)
                self.assertNotEqual(selected.resolution, "automatic-content-suggestion")
        self.assertEqual(self.transcribe.call_count, 2)

    def test_changed_audio_invalidates_cached_recommendation(self):
        previews = self._cache_checks()
        previews[1].path.write_bytes(b"replaced audio")
        selected = self._group(self._plan())
        self.assertEqual(selected.source_id, self.baseline.source_id)
        self.assertEqual(self.transcribe.call_count, 2)

    def test_different_actual_seeds_do_not_provide_comparable_evidence(self):
        self._cache_checks(seeds=(0, 3))
        selected = self._group(self._plan())
        self.assertEqual(selected.source_id, self.baseline.source_id)
        self.assertEqual(self.transcribe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
