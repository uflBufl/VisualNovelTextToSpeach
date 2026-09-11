import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
from vntts_artifacts import write_story_index_document

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.test_authoring_bulk_generation import audio_samples  # noqa: E402
from tests.test_generated_audio import FakeAudioOutput  # noqa: E402
from tests.test_pregeneration_setup import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_voices import write_manifest  # noqa: E402
from tests.test_self_service_pregeneration import (  # noqa: E402
    InProcessPocketGenerator,
)
from vntts.app import TrayApplication  # noqa: E402
from vntts.controller import AppController  # noqa: E402
from vntts.game_content_importer import ImporterAvailability  # noqa: E402
from vntts.generated_audio import (  # noqa: E402
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    PlaybackStatus,
)
from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.onboarding_ui import OnboardingWizard  # noqa: E402
from vntts.pregeneration_acceptance import OfflineAcceptanceWorker  # noqa: E402
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
from vntts.settings import AppSettings, load_app_settings  # noqa: E402
from vntts.voices import CharacterVoiceRegistry  # noqa: E402

PHASE_ENV = "VNTTS_FRESH_INSTALL_JOURNEY_PHASE"
ROOT_ENV = "VNTTS_FRESH_INSTALL_JOURNEY_ROOT"
TEST_ID = (
    "tests.test_fresh_install_gui_journey.FreshInstallGuiJourneyTest."
    "test_fresh_install_player_journey_survives_restart"
)
NARRATOR_LINE_ID = "fresh:narrator:1"
NARRATOR_TEXT = "The lantern still burns beside the door."


def _write_content(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "game_version": "test",
            "language": "en",
            "collections": [
                {
                    "collection_id": "selected-story",
                    "title": "Selected story",
                    "kind": "character-story",
                    "order": 1,
                },
                {
                    "collection_id": "unprepared-story",
                    "title": "Unprepared story",
                    "kind": "character-story",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": NARRATOR_LINE_ID,
                "chapter": "1",
                "sequence": 1,
                "speaker": "Narrator",
                "voice_character": "Narrator",
                "text": NARRATOR_TEXT,
                "kind": "narration",
                "collection_id": "selected-story",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "fresh:rhiannon:1",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "I will meet you outside.",
                "kind": "dialogue",
                "collection_id": "selected-story",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "fresh:hotelier:1",
                "chapter": "1",
                "sequence": 3,
                "speaker": "Hotelier",
                "voice_character": "Hotelier",
                "text": "Your room is ready.",
                "kind": "dialogue",
                "collection_id": "selected-story",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "fresh:rhiannon:2",
                "chapter": "2",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "This story remains available for later.",
                "kind": "dialogue",
                "collection_id": "unprepared-story",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path


def _write_valid_manifest(root):
    manifest = write_manifest(root)
    for index, reference in enumerate((root / "references").glob("*.wav"), start=1):
        samples = np.zeros(1_600, dtype=np.float32)
        samples[:: max(1, 20 * index)] = 0.1
        sf.write(reference, samples, 16_000, subtype="PCM_16")
    return manifest


def _run_next(pool, application):
    if not pool.tasks:
        raise AssertionError("The GUI journey stopped before reaching its next phase")
    pool.tasks.pop(0).run()
    application.processEvents()


def _wait_until(application, predicate, message, timeout_ms=10_000):
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        application.processEvents()
        QTest.qWait(5)
        elapsed += 5
    if not predicate():
        raise AssertionError(message)


def _first_process(root):
    application = QApplication.instance() or QApplication([])
    content = inspect_story_index(_write_content(root / "imported"))
    manifest = _write_valid_manifest(root / "voices")
    settings = AppSettings(
        speech_backend="pocket-tts",
        tts_profile="default",
        pocket_gated_model_accepted=True,
        voice_manifest=str(manifest),
        game_window_title="Reverse: 1999",
    )

    onboarding_controller = Mock(is_ready=False, is_live_running=False)
    onboarding_controller.apply_settings.return_value = True
    onboarding_controller.start.return_value = True
    onboarding_controller.test_current_dialog.return_value = ("Narrator", "Ready.")
    onboarding = TrayApplication(
        application,
        settings,
        controller_factory=Mock(return_value=onboarding_controller),
    )
    try:
        diagnostics = Mock()
        diagnostics.run.return_value = (
            DiagnosticResult("Fresh-install dependencies", "ok", "Ready"),
        )
        with patch(
            "vntts.app.OnboardingWizard",
            side_effect=lambda candidate, **options: OnboardingWizard(
                candidate,
                diagnostics=diagnostics,
                auto_discover_windows=False,
                **options,
            ),
        ):
            onboarding.run_onboarding()
            wizard = onboarding.onboarding_wizard
            if wizard is None:
                raise AssertionError("Onboarding did not open on a fresh install")
            wizard.configuration_page.game_window.setCurrentText("Reverse: 1999")
            wizard.next_button.click()
            _wait_until(
                application,
                lambda: wizard.diagnostics_page.complete,
                "Onboarding diagnostics did not finish",
            )
            wizard.next_button.click()
            wizard.calibration_page.finish_calibration(None)
            wizard.next_button.click()
            wizard.test_page.button.click()
            _wait_until(
                application,
                lambda: wizard.test_page.successful,
                "Onboarding OCR-to-speech test did not finish",
            )
            wizard.finish_button.click()
            application.processEvents()
            if onboarding.onboarding_wizard is not None:
                raise AssertionError(
                    "Onboarding did not finish through its Save button"
                )
    finally:
        onboarding.shutdown()

    saved = load_app_settings()
    if not saved.onboarding_completed:
        raise AssertionError("Onboarding completion was not persisted")

    jobs = PregenerationJobStore()
    decisions = VoiceDecisionStore(jobs.root.parent / "voice-decisions.json")
    voices = VoicePlanStore(jobs, decisions=decisions)
    inputs = PregenerationInputStore(jobs)
    generator = InProcessPocketGenerator()
    pool = ManualThreadPool()
    importer = Mock()
    importer.availability.return_value = ImporterAvailability(True, "Ready")
    importer.import_installed.return_value = content
    player = Mock()
    dialog = OfflineAudioPreparationDialog(
        saved,
        discovery=lambda: ContentDiscovery(()),
        importer=importer,
        job_store=jobs,
        voice_decisions=decisions,
        voice_plan_store=voices,
        input_store=inputs,
        generator=generator,
        recovery=OfflineRecoveryWorker(generator),
        acceptance=OfflineAcceptanceWorker(generator),
        preview_player=player,
        thread_pool=pool,
        automatic_activation=True,
    )
    phases = []
    dialog.phaseChanged.connect(phases.append)
    controller = Mock(is_ready=False, is_live_running=False)
    controller.apply_settings.return_value = True
    tray = TrayApplication(
        application,
        saved,
        controller_factory=Mock(return_value=controller),
    )
    try:
        with patch("vntts.app.OfflineAudioPreparationDialog", return_value=dialog):
            tray.open_pregeneration()

        dialog.import_button.click()
        if dialog.source.isEnabled():
            raise AssertionError("Import controls stayed enabled during import")
        _run_next(pool, application)
        if dialog.stories.count() != 2:
            raise AssertionError("Imported catalog did not show both stories")
        dialog.stories.item(1).setCheckState(Qt.CheckState.Unchecked)
        application.processEvents()
        if dialog.selected_story_ids() != ("selected-story",):
            raise AssertionError("The unprepared story was not left unselected")

        dialog.continue_button.click()
        for _ in range(4):
            if dialog._awaiting_voice_confirmation:
                break
            _run_next(pool, application)
        if not dialog._awaiting_voice_confirmation:
            raise AssertionError("Voice confirmation did not open")
        narrator_index = dialog.narrator_choice.findData("character:centurion")
        if narrator_index < 0:
            raise AssertionError("Centurion was not offered as a game narrator")
        dialog.narrator_choice.setCurrentIndex(narrator_index)
        dialog.play_narrator_reference.click()
        player.play.assert_called_once()
        if "Pocket TTS" not in dialog.narrator_status.text():
            raise AssertionError("The selected generation engine was not visible")

        dialog.continue_button.click()
        for _ in range(20):
            if dialog._awaiting_voice_confirmation and not pool.tasks:
                dialog.continue_button.click()
            if pool.tasks:
                _run_next(pool, application)
            else:
                application.processEvents()
            if dialog.pack_result() is not None:
                break
        if dialog.pack_result() is None:
            raise AssertionError(
                f"Preparation did not finish: {dialog.resume_status.text()}"
            )

        _wait_until(
            application,
            lambda: not tray.pregeneration_activation_runner.active,
            "Offline pack activation did not finish",
        )
        if tray.settings.game_pack != str(dialog.pack_result().manifest):
            raise AssertionError("The prepared pack was not activated")
        if tray.settings.last_main_section != "reading":
            raise AssertionError("Activation did not return to Reading")
        if controller.start.called:
            raise AssertionError("Preparation started playback automatically")
        if not any("Generating offline audio" in phase for phase in phases):
            raise AssertionError("Preparation did not expose generation progress")
    finally:
        tray.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()


def _second_process(root):
    application = QApplication.instance() or QApplication([])
    settings = load_app_settings()
    if not settings.onboarding_completed:
        raise AssertionError("Onboarding completion was lost after restart")
    if not settings.generated_audio_manifest or not settings.story_index:
        raise AssertionError("The active offline pack was not restored")

    controller = Mock(is_ready=False, is_live_running=False)
    controller.start.return_value = True
    tray = TrayApplication(
        application,
        None,
        controller_factory=Mock(return_value=controller),
    )
    try:
        forbidden_importer = Mock()
        forbidden_importer.availability.return_value = ImporterAvailability(
            False, "Import is forbidden after restart"
        )
        forbidden_importer.import_installed.side_effect = AssertionError(
            "Restart must not import game content again"
        )
        with (
            patch.object(tray.tray, "show"),
            patch(
                "vntts.pregeneration_ui.Reverse1999GameImporter",
                return_value=forbidden_importer,
            ),
        ):
            tray.start()
            _wait_until(
                application,
                lambda: (
                    tray.pregeneration_dialog is not None
                    and not tray.pregeneration_dialog.discovery_runner.active
                ),
                "Restart did not restore the story library",
            )
        stories = tray.pregeneration_dialog
        if stories is None or stories.stories.count() != 2:
            raise AssertionError("Restart lost stories that were not prepared")
        if stories.selected_story_ids() != ("selected-story",):
            raise AssertionError("Restart lost the saved story selection")
        forbidden_importer.import_installed.assert_not_called()
        with patch.object(tray, "start_hotkeys"):
            tray.prepare_reading()
            _wait_until(
                application,
                lambda: not tray.initial_start_runner.active,
                "Restart did not finish loading Reading",
            )
            QTest.qWait(300)
            application.processEvents()
            if tray.onboarding_wizard is not None:
                raise AssertionError("Restart opened onboarding again")
            controller.start.assert_called_once()

        registry = CharacterVoiceRegistry.from_file(settings.voice_manifest)
        narrator = registry.resolve("Narrator")
        rhiannon = registry.resolve("Rhiannon")
        if narrator is None or narrator.source_character != "Centurion":
            raise AssertionError("Restart lost the Centurion narrator binding")
        if rhiannon is None or rhiannon.source_character != "Rhiannon":
            raise AssertionError("Restart replaced the independent Rhiannon voice")

        live = Mock()
        live.name = "forbidden-live-tts"
        live.capabilities = Mock()
        live.prepare_playback.side_effect = AssertionError(
            "A prepared line must not invoke live synthesis"
        )
        output = FakeAudioOutput()
        runtime = AppController(
            settings,
            generated_audio_backend_factory=lambda source, library, resolver, **options: (
                GeneratedAudioFallbackBackend(
                    source, library, resolver, audio_output=output, **options
                )
            ),
        )
        runtime.speech_backend = live
        if not runtime._configure_generated_audio_backend():
            raise AssertionError("Production controller did not enable prepared audio")
        backend = runtime.speech_backend
        route = backend.prepare_route(
            "Narrator", NARRATOR_TEXT, line_id=NARRATOR_LINE_ID
        )
        if not isinstance(route, GeneratedAudioRoute):
            raise AssertionError(f"Prepared line selected {type(route).__name__}")
        outcome = backend.play_route(route)
        if outcome.status is not PlaybackStatus.COMPLETED:
            raise AssertionError(f"Prepared playback failed: {outcome.status}")
        if len(output.plays) != 1 or output.plays[0][1] != 16_000:
            raise AssertionError("Prepared PCM did not reach the recording audio sink")
        if not np.allclose(output.plays[0][0], audio_samples(), atol=1 / 32_768):
            raise AssertionError("Playback did not use the independently expected PCM")
        live.prepare_playback.assert_not_called()
    finally:
        tray.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()


class FreshInstallGuiJourneyTest(unittest.TestCase):
    def test_fresh_install_player_journey_survives_restart(self):
        phase = os.environ.get(PHASE_ENV)
        if phase:
            root = Path(os.environ[ROOT_ENV])
            {"first": _first_process, "second": _second_process}[phase](root)
            return

        with TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "QT_QPA_PLATFORM": "offscreen",
                "HOME": str(root / "home"),
                "APPDATA": str(root / "config"),
                "LOCALAPPDATA": str(root / "data"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "VNTTS_SETTINGS_FILE": str(root / "settings.json"),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                ROOT_ENV: str(root),
            }
            for phase in ("first", "second"):
                completed = subprocess.run(
                    [sys.executable, "-m", "unittest", TEST_ID],
                    cwd=Path(__file__).parents[1],
                    env={**environment, PHASE_ENV: phase},
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{phase} process failed:\n{completed.stdout}\n{completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
