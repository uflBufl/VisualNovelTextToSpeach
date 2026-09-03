import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tests.test_authoring_bulk_generation import SyntheticRenderer  # noqa: E402
from tests.test_pregeneration_setup import (  # noqa: E402
    ManualThreadPool,
    write_story_index,
)
from tests.test_pregeneration_voices import (  # noqa: E402
    write_conflicting_manifest,
    write_content,
)
from vntts.app import TrayApplication  # noqa: E402
from vntts.authoring.bulk_generation import run_bulk_generation  # noqa: E402
from vntts.authoring.missing_voice_policy import (  # noqa: E402
    NARRATOR_ROLES,
    MissingVoicePolicy,
)
from vntts.pregeneration_acceptance import OfflineAcceptanceWorker  # noqa: E402
from vntts.pregeneration_activation import OfflinePackActivator  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionCancelled  # noqa: E402
from vntts.pregeneration_generation import (  # noqa: E402
    OfflineGenerationCancelled,
    OfflineGenerationWorker,
)
from vntts.pregeneration_queue import PregenerationInputStore  # noqa: E402
from vntts.pregeneration_recovery import OfflineRecoveryWorker  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import (  # noqa: E402
    VoiceDecisionStore,
    VoicePlanStore,
)
from vntts.settings import AppSettings  # noqa: E402
from vntts.synthesis import SynthesisCompletion  # noqa: E402


class InProcessPocketGenerator(OfflineGenerationWorker):
    def __init__(self):
        super().__init__()
        self.rendered = False

    def generate(self, generation_input, voice_plan, cancel_event=None):
        output = generation_input.directory.parent / (
            f"generation-output-{generation_input.identity[:16]}"
        )
        renderer = SyntheticRenderer(
            [SynthesisCompletion.COMPLETE, SynthesisCompletion.LIMITED]
        )
        renderer.name = "pocket-tts"
        renderer.model_name = "pocket-tts"
        run_bulk_generation(
            generation_input.queue,
            output,
            renderer,
            provider="pocket-tts",
            model="pocket-tts",
            generation_profile=voice_plan.synthesis_profile,
            retries=0,
            cancellation=cancel_event,
            missing_voice_policy=MissingVoicePolicy(
                NARRATOR_ROLES,
                generation_input.narrator_fallback_roles,
            ),
            narrator_character="Narrator",
        )
        self.rendered = True
        return self.inspect(generation_input)


class InterruptingPocketGenerator(InProcessPocketGenerator):
    def __init__(self, *, interrupt):
        super().__init__()
        self.interrupt = interrupt
        self.rendered_texts = []

    def generate(self, generation_input, voice_plan, cancel_event=None):
        output = generation_input.directory.parent / (
            f"generation-output-{generation_input.identity[:16]}"
        )
        renderer = SyntheticRenderer(
            (
                [SynthesisCompletion.COMPLETE, SynthesisCompletion.CANCELLED]
                if self.interrupt
                else [SynthesisCompletion.COMPLETE]
            )
        )
        renderer.name = "pocket-tts"
        renderer.model_name = "pocket-tts"
        run_bulk_generation(
            generation_input.queue,
            output,
            renderer,
            provider="pocket-tts",
            model="pocket-tts",
            generation_profile="default",
            retries=0,
            cancellation=cancel_event,
            missing_voice_policy=MissingVoicePolicy(
                NARRATOR_ROLES,
                generation_input.narrator_fallback_roles,
            ),
            narrator_character="Narrator",
        )
        self.rendered_texts.extend(request.text for request in renderer.requests)
        if self.interrupt:
            raise OfflineGenerationCancelled("Synthetic generation interrupted")
        self.rendered = True
        return self.inspect(generation_input)


class SelfServicePregenerationJourneyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_zero_ambiguity_story_reaches_an_active_portable_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            inputs = PregenerationInputStore(jobs)
            generator = InProcessPocketGenerator()
            recovery = OfflineRecoveryWorker(generator)
            acceptance = OfflineAcceptanceWorker(generator)
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=generator,
                recovery=recovery,
                acceptance=acceptance,
                thread_pool=pool,
            )
            visible_text = [dialog.summary.text(), dialog.resume_status.text()]

            dialog.continue_button.click()
            for _step in range(6):
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {dialog.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()
                visible_text.extend(
                    (
                        dialog.summary.text(),
                        dialog.resume_status.text(),
                        dialog.cancel_button.text(),
                    )
                )

            self.assertEqual(dialog.progress_phase.text(), "Offline audio is ready")
            self.assertEqual(dialog.continue_button.text(), "Use prepared audio")
            dialog.continue_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertTrue(generator.rendered)
            self.assertEqual(dialog.voice_plan().audition_count, 0)
            self.assertEqual(dialog.recovery_result().live_fallbacks, 1)
            self.assertEqual(dialog.acceptance_result().approved, 1)
            self.assertEqual(dialog.pack_result().approved, 1)
            self.assertEqual(dialog.pack_result().live_fallbacks, 1)
            player_copy = " ".join(visible_text).casefold()
            for authoring_term in (
                "workspace",
                "queue id",
                "manifest",
                "checksum",
                "seed",
                "per-line review",
            ):
                self.assertNotIn(authoring_term, player_copy)

            saved_settings = root / "settings.json"
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            tray = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                pregeneration_activator=OfflinePackActivator(
                    save_settings=lambda settings: settings.save(saved_settings)
                ),
            )
            completed = Mock()
            completed.exec.return_value = QDialog.DialogCode.Accepted
            completed.job.return_value = dialog.job()
            completed.voice_plan.return_value = dialog.voice_plan()
            completed.generation_input.return_value = dialog.generation_input()
            completed.generation_result.return_value = dialog.generation_result()
            completed.recovery_result.return_value = dialog.recovery_result()
            completed.acceptance_result.return_value = dialog.acceptance_result()
            completed.pack_result.return_value = dialog.pack_result()

            with patch(
                "vntts.app.OfflineAudioPreparationDialog",
                return_value=completed,
            ):
                self.assertIs(tray.open_pregeneration(), dialog.job())
            for _attempt in range(400):
                self.application.processEvents()
                if not tray.pregeneration_activation_runner.active:
                    break
                QTest.qWait(5)

            self.assertFalse(tray.pregeneration_activation_runner.active)
            self.assertEqual(tray.settings.audio_source_policy, "prefer-generated")
            self.assertEqual(
                tray.settings.game_pack, str(dialog.pack_result().manifest)
            )
            self.assertTrue(saved_settings.is_file())
            self.assertIn("Offline audio is active", tray.dashboard.status.text())
            controller.start.assert_not_called()
            tray.shutdown()
            dialog.deleteLater()

    def test_ambiguous_voice_choice_resumes_then_completes_without_line_review(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_conflicting_manifest(root / "voices")
            for reference in (manifest.parent / "references").glob("*.wav"):
                sf.write(
                    reference,
                    np.zeros(1_600, dtype=np.float32),
                    16_000,
                    subtype="PCM_16",
                )
            settings = AppSettings(voice_manifest=str(manifest))
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            pool = ManualThreadPool()
            cancelled_preview = Mock()
            cancelled_preview.generate.side_effect = VoiceAuditionCancelled("cancelled")
            first = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                voice_decisions=decisions,
                audition_service=cancelled_preview,
                preview_player=Mock(),
                thread_pool=pool,
            )

            first.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertTrue(first.auditioning_voices)
            interrupted_job_id = first.job().job_id
            first.cancel_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(first.result(), QDialog.DialogCode.Rejected)

            preview = Mock()
            preview.generate.side_effect = lambda _plan, _group, source_id: Mock(
                path=root / f"{source_id.removeprefix('character:')}.wav"
            )
            generator = InProcessPocketGenerator()
            second = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                voice_decisions=decisions,
                audition_service=preview,
                preview_player=Mock(),
                input_store=PregenerationInputStore(jobs),
                generator=generator,
                recovery=OfflineRecoveryWorker(generator),
                acceptance=OfflineAcceptanceWorker(generator),
                thread_pool=pool,
            )

            second.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(second.job().job_id, interrupted_job_id)
            self.assertTrue(second.auditioning_voices)
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertTrue(second.voice_panel.a_use.isEnabled())
            second.voice_panel.a_use.click()
            for _step in range(8):
                if second.pack_result() is not None:
                    break
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {second.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()

            self.assertEqual(second.progress_phase.text(), "Offline audio is ready")
            second.continue_button.click()
            self.assertEqual(second.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(second.voice_plan().audition_count, 0)
            self.assertTrue(generator.rendered)
            self.assertTrue(decisions.path.is_file())
            first.deleteLater()
            second.deleteLater()

    def test_process_restart_resumes_only_the_cancelled_line(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            settings = AppSettings(
                speech_backend="pocket-tts",
                tts_profile="default",
            )
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            inputs = PregenerationInputStore(jobs)
            pool = ManualThreadPool()
            interrupted = InterruptingPocketGenerator(interrupt=True)
            first = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=interrupted,
                thread_pool=pool,
            )

            first.continue_button.click()
            for _step in range(3):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            interrupted_job_id = first.job().job_id
            interrupted_input_id = first.generation_input().identity
            self.assertIn("Generation cancelled", first.resume_status.text())
            self.assertEqual(first.progress_phase.text(), "Generation paused")
            self.assertIn("2 of 2", first.progress_counts.text())
            self.assertIn(
                "generate only unfinished lines",
                first.progress_cancel_consequence.text(),
            )
            self.assertEqual(len(interrupted.rendered_texts), 2)
            first.reject()
            first.deleteLater()

            resumed = InterruptingPocketGenerator(interrupt=False)
            second = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=resumed,
                recovery=OfflineRecoveryWorker(resumed),
                acceptance=OfflineAcceptanceWorker(resumed),
                thread_pool=pool,
            )
            second.continue_button.click()
            for _step in range(8):
                if second.pack_result() is not None:
                    break
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {second.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()

            self.assertEqual(second.progress_phase.text(), "Offline audio is ready")
            second.continue_button.click()
            self.assertEqual(second.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(second.job().job_id, interrupted_job_id)
            self.assertEqual(second.generation_input().identity, interrupted_input_id)
            self.assertEqual(resumed.rendered_texts, interrupted.rendered_texts[-1:])
            self.assertNotIn(interrupted.rendered_texts[0], resumed.rendered_texts)
            self.assertEqual(second.pack_result().approved, 2)
            self.assertEqual(second.pack_result().live_fallbacks, 0)
            second.deleteLater()


if __name__ == "__main__":
    unittest.main()
