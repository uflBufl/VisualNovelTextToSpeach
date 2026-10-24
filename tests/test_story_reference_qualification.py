import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from tests.pregeneration_fixtures import FakeBackend, clean_wav_bytes
from tests.story_fixtures import write_content
from tests.voice_manifest_fixtures import write_player_candidate_manifest
from vntts.document_identity import sha256_file
from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    PregenerationVoiceCancelled,
    PregenerationVoiceError,
    VoicePlanStore,
)
from vntts.preview_content_advisor import PreviewContentAdvisor
from vntts.settings import AppSettings
from vntts.story_reference_qualification import qualify_story_references
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoiceRegistry, remember_voice_binding


class StoryReferenceQualificationTest(unittest.TestCase):
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
        document = json.loads(self.manifest.read_text())
        for index, (voice, variant) in enumerate(
            zip(
                document["voices"],
                document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"],
                strict=True,
            )
        ):
            reference = self.manifest.parent / voice["references"][0]
            reference.write_bytes(
                clean_wav_bytes(amplitude=0.1 + index * 0.05, seconds=3)
            )
            variant["reference_sha256"] = sha256_file(reference)
        self.manifest.write_text(json.dumps(document))
        self.settings = AppSettings(
            speech_backend="coqui-xtts", tts_language="en", xtts_terms_accepted=True
        )
        self.initial = VoicePlanStore(self.jobs).create(
            self.job, self.settings, manifest_path=self.manifest
        )
        self.group = next(g for g in self.initial.groups if g.character == "Rhiannon")
        self.baseline, self.alternative = self.group.candidates
        self.backend = FakeBackend(
            "coqui-xtts", pcm=np.tile(np.array([0.1, -0.1], dtype=np.float32), 24_000)
        )
        self.previewer = VoiceAuditionPreviewService(
            self.root / "previews",
            backend_factory=lambda *_args, **_kwargs: self.backend,
        )
        self.addCleanup(self.previewer.close)
        self.transcribe = Mock(
            side_effect=[COMPARISON_PREVIEW_TEXT + " Wine.", COMPARISON_PREVIEW_TEXT]
        )
        self.advisor = PreviewContentAdvisor(
            self.previewer.root,
            transcriber_factory=lambda _: self.transcribe,
            model_resolver=lambda: self.root / "model",
            model_status=lambda: {"status": "ready"},
        )
        self.cancellation = Event()
        self.progress = []

    def qualify(self, plan):
        return qualify_story_references(
            plan,
            preview_service=self.previewer,
            content_advisor=self.advisor,
            cancellation=self.cancellation,
            progress=self.progress.append,
        )

    def create(self, *, library=None, qualifier=None):
        return VoicePlanStore(self.jobs, voice_library=library).create(
            self.job,
            self.settings,
            manifest_path=self.manifest,
            cancellation=self.cancellation,
            reference_qualifier=qualifier or self.qualify,
        )

    def test_cold_plan_qualifies_before_publication_and_saves_automatic_source(self):
        library = VoiceLibrary(self.root / "library")
        store = VoicePlanStore(self.jobs)
        prior_plan = store.saved_documents()

        def qualify(plan):
            self.assertIsNone(library.binding("Rhiannon"))
            self.assertEqual(store.saved_documents(), prior_plan)
            result = self.qualify(plan)
            self.assertIsNone(library.binding("Rhiannon"))
            self.assertEqual(store.saved_documents(), prior_plan)
            return result

        plan = self.create(library=library, qualifier=qualify)
        group = next(g for g in plan.groups if g.group_id == self.group.group_id)
        self.assertEqual(group.source_id, self.alternative.source_id)
        self.assertEqual(group.resolution, "automatic-content-suggestion")
        binding = library.binding("Rhiannon")
        self.assertEqual(binding.source_sha256s, self.alternative.reference_sha256s)
        self.assertEqual(binding.provenance["method"], "automatic")
        self.assertEqual(binding.provenance["algorithm"], "voice-plan-v3-qualified")
        saved = store.saved_documents()[0]
        saved_group = next(
            g for g in saved["groups"] if g["group_id"] == group.group_id
        )
        self.assertEqual(saved_group["source_id"], self.alternative.source_id)
        self.assertNotEqual(group.control_sha256, self.group.control_sha256)
        self.assertEqual(self.group.source_id, self.baseline.source_id)
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.transcribe.call_count, 2)
        self.assertTrue(self.progress)

    def test_exact_warm_plan_reuses_preview_and_asr_without_inference(self):
        self.assertEqual(
            self.qualify(self.initial)[self.group.group_id], self.alternative.source_id
        )
        self.backend.on_render = lambda: self.fail("warm plan rendered audio")
        self.transcribe.side_effect = AssertionError("warm plan loaded ASR")
        self.assertEqual(
            self.qualify(self.initial)[self.group.group_id], self.alternative.source_id
        )
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.transcribe.call_count, 2)
        changed = replace(self.initial, synthesis_profile="natural")
        self.qualify(changed)
        self.assertGreater(len(self.backend.requests), 2)

    def test_checker_failure_keeps_baseline_without_false_bad_evidence(self):
        self.transcribe.side_effect = RuntimeError("checker unavailable")
        selected = self.qualify(self.initial)
        self.assertEqual(selected[self.group.group_id], self.baseline.source_id)
        self.assertTrue(any("unavailable" in message for message in self.progress))
        self.assertFalse(tuple((self.advisor.root / "content-advice").glob("*.json")))

    def test_cancellation_rolls_back_and_leaves_existing_plan_unchanged(self):
        library = VoiceLibrary(self.root / "library")
        store = VoicePlanStore(self.jobs)
        prior_plan = store.saved_documents()
        self.backend.on_render = self.cancellation.set
        with self.assertRaises(PregenerationVoiceCancelled):
            self.create(library=library)
        self.assertIsNone(library.binding("Rhiannon"))
        self.assertEqual(store.saved_documents(), prior_plan)
        self.assertEqual(self.transcribe.call_count, 0)

    def test_saved_manual_and_automatic_choices_do_not_get_requalified(self):
        for method in ("manual", "automatic"):
            with self.subTest(method=method):
                library = VoiceLibrary(self.root / method)
                remember_voice_binding(
                    library,
                    CharacterVoiceRegistry.from_file(self.manifest),
                    "Rhiannon",
                    self.baseline.source_id,
                    method=method,
                )
                before = library.binding("Rhiannon")
                self.create(library=library)
                self.assertEqual(library.binding("Rhiannon"), before)
        self.assertEqual(len(self.backend.requests), 0)
        self.assertEqual(self.transcribe.call_count, 0)

    def test_concurrent_manual_choice_survives_qualification_without_wrong_plan(self):
        library = VoiceLibrary(self.root / "library")

        def qualify(plan):
            selections = self.qualify(plan)
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(self.manifest),
                "Rhiannon",
                self.baseline.source_id,
                method="manual",
            )
            return selections

        with self.assertRaisesRegex(PregenerationVoiceError, "changed during"):
            self.create(library=library, qualifier=qualify)
        binding = library.binding("Rhiannon")
        self.assertEqual(binding.provenance["method"], "manual")
        self.assertEqual(binding.source_sha256s, self.baseline.reference_sha256s)

    def test_strong_identity_is_automatic_but_uncertain_identity_still_needs_audition(
        self,
    ):
        strong = replace(
            self.group, route="needs-audition", resolution="ambiguous-voice-evidence"
        )
        self.assertIn(
            strong.group_id, self.qualify(replace(self.initial, groups=(strong,)))
        )
        weak = replace(
            strong,
            candidates=tuple(replace(c, match_score=60) for c in strong.candidates),
        )
        self.assertEqual(self.qualify(replace(self.initial, groups=(weak,))), {})

    def test_unsupported_language_does_not_generate_checker_only_audio(self):
        plan = replace(self.initial, synthesis_language="ru")
        self.assertEqual(
            self.qualify(plan)[self.group.group_id], self.baseline.source_id
        )
        self.assertEqual(len(self.backend.requests), 0)
        self.assertEqual(self.transcribe.call_count, 0)

    def test_qualification_never_attempts_more_than_three_references_per_group(self):
        group = replace(
            self.group,
            candidates=(
                *self.group.candidates,
                replace(self.alternative, source_id="character:third"),
                replace(self.alternative, source_id="character:fourth"),
            ),
        )
        previewer = Mock(wraps=self.previewer)
        selections = qualify_story_references(
            replace(self.initial, groups=(group,)),
            preview_service=previewer,
            content_advisor=self.advisor,
        )
        self.assertEqual(previewer.generate.call_count, 3)
        self.assertEqual(selections[group.group_id], self.alternative.source_id)

    def _dialog_qualify(self, panel):
        from vntts.pregeneration_ui import OfflineAudioPreparationDialog

        context = SimpleNamespace(
            voice_panel=panel,
            voice_cancel_event=self.cancellation,
            decoderProgress=SimpleNamespace(emit=self.progress.append),
        )
        return OfflineAudioPreparationDialog._qualify_story_references(
            context, self.initial
        )

    def test_inspector_back_does_not_close_the_next_story_planning_session(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from tests.qt_task_fixtures import ManualThreadPool
        from vntts.pregeneration_audition_queue import VoiceAuditionCoordinator
        from vntts.pregeneration_audition_ui import VoiceAuditionPanel

        type(self).application = QApplication.instance() or QApplication([])
        owner = VoiceAuditionCoordinator(self.previewer)
        self.addCleanup(owner.close)
        inspector = owner.session()
        panel = VoiceAuditionPanel(
            Mock(),
            preview_service=inspector,
            content_advisor=self.advisor,
            thread_pool=ManualThreadPool(),
        )
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.shutdown)
        panel.start(self.initial, group_id=self.group.group_id)
        panel._emit_cancelled()
        self.assertTrue(inspector._closed)

        selections = self._dialog_qualify(panel)

        self.assertEqual(selections[self.group.group_id], self.alternative.source_id)
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.transcribe.call_count, 2)
        self.assertEqual(owner._sessions, set())
        self.assertEqual(self.backend.shutdown_count, 0)

    def test_closed_standalone_renderer_is_recreated_for_planning(self):
        self.previewer.close()
        panel = SimpleNamespace(
            preview_service=self.previewer, content_advisor=self.advisor
        )
        selections = self._dialog_qualify(panel)
        self.assertEqual(selections[self.group.group_id], self.alternative.source_id)
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.backend.shutdown_count, 1)

    def test_planning_does_not_close_a_simple_injected_renderer(self):
        renderer = SimpleNamespace(generate=self.previewer.generate, close=Mock())
        panel = SimpleNamespace(preview_service=renderer, content_advisor=self.advisor)
        selections = self._dialog_qualify(panel)
        self.assertEqual(selections[self.group.group_id], self.alternative.source_id)
        renderer.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
