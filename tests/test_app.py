import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import ModuleType
from unittest.mock import ANY, Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QAction, QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QSizePolicy,
)
from shiboken6 import isValid  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.app import (  # noqa: E402
    SettingsDialog,
    TrayApplication,
    build_story_match_recovery_prompt,
    create_application_icon,
    main,
)
from vntts.async_ui import LatestTaskRunner  # noqa: E402
from vntts.cli import CLIReportResult  # noqa: E402
from vntts.controller import AppController, LiveSequenceStatus  # noqa: E402
from vntts.diagnostics import DiagnosticSnapshot  # noqa: E402
from vntts.generated_audio import AudioRouteTrace  # noqa: E402
from vntts.ocr import DialogRegion  # noqa: E402
from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.pregeneration_activation import OfflinePackActivationResult  # noqa: E402
from vntts.pregeneration_pack import OfflinePackResult  # noqa: E402
from vntts.pregeneration_voices import resolve_pregeneration_settings  # noqa: E402
from vntts.profiles import GameProfileStore  # noqa: E402
from vntts.settings import (  # noqa: E402
    AppSettings,
    load_app_settings,
    settings_schema_version,
)
from vntts.versioned_json import read_versioned_json_snapshot  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


def delete_dialog(dialog):
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


class ImmediateTaskPool(QThreadPool):
    def start(self, runnable: QRunnable, priority: int = 0) -> None:
        runnable.run()


class TrayApplicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        settings_directory = TemporaryDirectory()
        self.addCleanup(settings_directory.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "VNTTS_SETTINGS_FILE": str(
                    Path(settings_directory.name) / "settings.json"
                )
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def wait_until(self, predicate, *, timeout_ms=2000):
        for _ in range(max(1, timeout_ms // 5)):
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for an asynchronous UI operation")

    def test_successful_readiness_returns_to_reading_tab(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        with patch(
            "vntts.app.OnboardingDiagnostics.run",
            return_value=(DiagnosticResult("Audio output", "ok", "Speakers"),),
        ):
            tray.open_readiness()
            self.wait_until(lambda: tray.readiness_dialog.reading_button.isVisible())
        tray.dashboard.show_stories()
        tray.readiness_dialog.reading_button.click()
        self.assertEqual(tray.dashboard.sections.currentIndex(), 2)
        self.assertTrue(tray.dashboard.isVisible())
        self.assertFalse(tray.readiness_dialog.isVisible())
        tray.shutdown()
        delete_dialog(tray.readiness_dialog)
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_voice_remediation_closes_readiness_before_opening_voices(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        with (
            patch(
                "vntts.app.OnboardingDiagnostics.run",
                return_value=(
                    DiagnosticResult(
                        "Character voices", "warning", "Narrator fallback", "voices"
                    ),
                ),
            ),
            patch.object(
                tray,
                "open_voice_previews",
                side_effect=lambda: (
                    tray.show_dashboard(),
                    tray.dashboard.show_voices(),
                ),
            ) as voices,
        ):
            tray.open_readiness()
            self.wait_until(
                lambda: tray.readiness_dialog.remediation_button.isVisible()
            )
            tray.readiness_dialog.remediation_button.click()
        voices.assert_called_once_with()
        self.assertFalse(tray.readiness_dialog.isVisible())
        self.assertEqual(tray.dashboard.sections.currentIndex(), 1)
        tray.shutdown()
        delete_dialog(tray.readiness_dialog)
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_application_owns_retained_pocket_runtime(self):
        runtime = Mock()
        tray = TrayApplication(
            self.application,
            AppSettings(),
            pocket_runtime=runtime,
        )

        self.assertIs(tray.controller.pocket_backend_factory, runtime)
        tray.shutdown()
        runtime.shutdown.assert_called_once_with()
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_quit_waits_for_live_worker_before_closing_retained_runtime(self):
        started = Event()
        release = Event()
        retained_closed = Event()
        used_closed_runtime = Event()
        moss_runtime = Mock(loaded=False)
        moss_runtime.shutdown.side_effect = retained_closed.set
        pocket_runtime = Mock()
        controller = AppController(AppSettings())
        backend = Mock()
        controller.tts = backend
        controller.live_reader = Mock()
        controller.live_reader.wait.side_effect = TimeoutError("reader did not stop")
        executor = ThreadPoolExecutor(max_workers=1)
        controller.speech_executor = executor

        def blocked_worker():
            started.set()
            release.wait()
            if retained_closed.is_set():
                used_closed_runtime.set()
            backend.prepare_playback("Ada", "Still preparing")

        executor.submit(blocked_worker)
        self.assertTrue(started.wait(1))
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
            moss_runtime=moss_runtime,
            pocket_runtime=pocket_runtime,
        )
        try:
            tray.shutdown()
            self.assertFalse(retained_closed.is_set())
            self.assertFalse(controller.shutdown_complete.is_set())
        finally:
            release.set()

        self.assertTrue(controller.shutdown_complete.wait(1))
        self.assertTrue(retained_closed.wait(1))
        self.assertFalse(used_closed_runtime.is_set())
        moss_runtime.shutdown.assert_called_once_with()
        pocket_runtime.shutdown.assert_called_once_with()
        backend.shutdown.assert_called_once_with()
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_live_compute_tracks_backend_replacement_without_technical_details(self):
        controller = Mock()
        controller.speech_backend.runtime_status = "GPU: RTX 2070 SUPER"
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        self.assertIn("GPU: RTX 2070 SUPER", tray.dashboard.speech_runtime.text())
        controller.speech_backend = None
        tray._refresh_speech_runtime()
        self.assertIn("not loaded", tray.dashboard.speech_runtime.text())
        tray.shutdown()
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_load_openmoss_installs_missing_managed_runtime_after_confirmation(self):
        controller = Mock(is_live_running=False)
        controller.voice_registry_initializer.return_value = object()
        runtime = Mock(loaded=False)
        tray = TrayApplication(
            self.application,
            AppSettings(speech_backend="moss-tts"),
            controller_factory=Mock(return_value=controller),
            moss_runtime=runtime,
        )
        tray.moss_runtime_runner = Mock(active=False)

        with (
            patch(
                "vntts.app.OnboardingDiagnostics.moss_installation_space",
                return_value=(17_390_235, 151_607_963, 20_000_000_000),
            ),
            patch(
                "vntts.app.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as question,
        ):
            tray.toggle_moss_runtime()

        self.assertIn("17.4 MB", question.call_args.args[2])
        self.assertTrue(
            tray.moss_runtime_runner.start.call_args.kwargs["allow_download"]
        )
        tray.shutdown()
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_missing_saved_pack_opens_settings_without_starting_playback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            saved = AppSettings(
                game_pack=str(root / "missing-game-pack.json"),
                onboarding_completed=True,
                compact_controls=True,
            )
            saved.save(path)
            controller = Mock(is_ready=False)
            with (
                patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}),
                patch("vntts.app.get_local_data_directory", return_value=root),
                patch(
                    "vntts.app.QSystemTrayIcon.isSystemTrayAvailable",
                    return_value=False,
                ),
            ):
                tray_application = TrayApplication(
                    self.application,
                    controller_factory=Mock(return_value=controller),
                    profile_store=GameProfileStore(root / "profiles.json"),
                )
                tray_application.start()
                self.assertTrue(tray_application.dashboard.isVisible())
                self.assertTrue(tray_application.settings_action.isEnabled())
                self.assertFalse(tray_application.read_action.isEnabled())
                self.assertFalse(tray_application.live_action.isEnabled())
                self.assertEqual(tray_application.settings.game_pack, saved.game_pack)
                self.assertIn("Open Settings", tray_application.status_action.text())
                controller.prepare_startup.assert_not_called()
                controller.start.assert_not_called()
                self.assertIsNone(tray_application.hotkey_listener)
                tray_application.shutdown()
                delete_dialog(tray_application.dashboard)
                delete_dialog(tray_application.compact_controller)

    def test_tray_menu_and_commands_follow_their_native_owner_lifetimes(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        menu = tray.menu
        actions = [value for value in vars(tray).values() if isinstance(value, QAction)]
        self.assertTrue(actions)

        tray.shutdown()
        tray.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(tray.tray))
        self.assertFalse(isValid(menu))
        self.assertTrue(all(isValid(action) for action in actions))

        tray.deleteLater()
        QCoreApplication.sendPostedEvents(tray, QEvent.Type.DeferredDelete)
        self.assertTrue(all(not isValid(action) for action in actions))
        delete_dialog(tray.dashboard)
        delete_dialog(tray.compact_controller)

    def test_tray_shell_exposes_runtime_controls(self):
        controller = Mock()
        controller_factory = Mock(return_value=controller)

        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )

        self.assertEqual(tray_application.read_action.text(), "Read current dialogue")
        self.assertEqual(tray_application.dialog_action.text(), "No dialogue detected")
        self.assertEqual(tray_application.show_dashboard_action.text(), "Full controls")
        self.assertEqual(
            tray_application.show_compact_action.text(), "Compact controls"
        )
        self.assertEqual(tray_application.live_action.text(), "Start reading")
        self.assertEqual(
            tray_application.calibrate_action.text(),
            "Calibrate dialogue region...",
        )
        self.assertEqual(
            tray_application.diagnostics_action.text(),
            "Live diagnostics",
        )
        self.assertEqual(tray_application.readiness_action.text(), "Check readiness")
        self.assertEqual(tray_application.setup_action.text(), "Run setup")
        self.assertEqual(tray_application.profiles_action.text(), "Game profiles...")
        self.assertEqual(
            tray_application.corrections_action.text(),
            "OCR corrections...",
        )
        self.assertEqual(
            tray_application.ocr_review_action.text(),
            "Review uncertain OCR...",
        )
        self.assertEqual(
            tray_application.pregeneration_action.text(),
            "Prepare offline audio...",
        )
        self.assertEqual(
            tray_application.assets_action.text(),
            "Manage models and voices...",
        )
        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.voice_preview_action.text(),
            "Choose narrator voice...",
        )
        self.assertEqual(tray_application.history_action.text(), "Dialogue history...")
        self.assertEqual(
            tray_application.support_action.text(),
            "Support and logs",
        )
        self.assertIs(tray_application.tray.parent(), tray_application)
        self.assertEqual(
            tray_application.macos_permissions_action.text(),
            "macOS permissions...",
        )
        tray_destroyed = []
        tray_application.tray.destroyed.connect(lambda: tray_destroyed.append(True))
        self.assertFalse(tray_application.read_action.isEnabled())
        self.assertFalse(tray_application.live_action.isEnabled())
        self.assertFalse(tray_application.sequence_resync_action.isVisible())
        top_level = {
            action.text()
            for action in tray_application.menu.actions()
            if not action.isSeparator()
        }
        self.assertIn("Reading", top_level)
        self.assertIn("Setup", top_level)
        self.assertIn("Support", top_level)
        self.assertNotIn("Settings...", top_level)
        self.assertNotIn(
            "Emergency stop",
            {action.text() for action in tray_application.playback_menu.actions()},
        )
        self.assertIn(
            tray_application.readiness_action,
            tray_application.setup_menu.actions(),
        )
        self.assertIn(
            tray_application.diagnostics_action,
            tray_application.support_menu.actions(),
        )
        tray_application.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(tray_destroyed, [True])
        controller.shutdown.assert_called_once_with()

    def test_runtime_capabilities_match_all_three_control_surfaces(self):
        controller = Mock()
        controller.is_live_running = False
        controller.live_reader.runtime_control_snapshot.return_value = {}
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application._controller_ready = True
        cases = (
            ("idle", False, {}, (True, True)),
            ("live", True, {}, (False, True)),
            (
                "speaking",
                False,
                {"speaking": True},
                (False, True),
            ),
            (
                "paused",
                False,
                {"paused": True},
                (False, True),
            ),
            (
                "queued",
                False,
                {"queued": True},
                (False, True),
            ),
            (
                "replayable",
                False,
                {"replayable": True},
                (True, True),
            ),
        )
        for name, live, snapshot, expected in cases:
            with self.subTest(state=name):
                controller.is_live_running = live
                controller.live_reader.runtime_control_snapshot.return_value = snapshot
                tray_application._apply_controller_action_state()
                tray_state = tuple(
                    action.isEnabled()
                    for action in (
                        tray_application.read_action,
                        tray_application.live_action,
                    )
                )
                dashboard_state = tuple(
                    button.isEnabled()
                    for button in (
                        tray_application.dashboard.read_button,
                        tray_application.dashboard.live_button,
                    )
                )
                compact_state = tuple(
                    button.isEnabled()
                    for button in (
                        tray_application.compact_controller.read_button,
                        tray_application.compact_controller.live_button,
                    )
                )
                self.assertEqual(tray_state, expected)
                self.assertEqual(dashboard_state, expected)
                self.assertEqual(compact_state, expected)
                self.assertEqual(
                    tray_application.live_action.text(),
                    "Stop reading"
                    if name in {"live", "speaking", "paused", "queued"}
                    else "Start reading",
                )
        tray_application.shutdown()

    def test_status_refresh_preserves_voice_picker_action_lock(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock(is_live_running=False)),
        )
        tray_application._controller_ready = True
        tray_application.narrator_dialog = Mock()
        tray_application._apply_controller_action_state()
        self.assertFalse(tray_application.voice_preview_action.isEnabled())

        tray_application.set_status("Choose a voice in Voices.")

        self.assertFalse(tray_application.voice_preview_action.isEnabled())
        tray_application.narrator_dialog = None
        tray_application.shutdown()

    def test_tray_non_action_text_is_bounded_without_losing_full_tooltip(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        long_status = "Private diagnostic detail " * 20
        long_dialogue = "A private dialogue sentence " * 20

        tray_application.set_status(long_status)
        tray_application.set_dialog("An especially long speaker", long_dialogue)

        self.assertLessEqual(len(tray_application.status_action.text()), 96)
        self.assertLessEqual(len(tray_application.dialog_action.text()), 96)
        self.assertEqual(tray_application.status_action.toolTip(), long_status)
        self.assertIn(long_dialogue, tray_application.dialog_action.toolTip())
        tray_application.set_status(None)
        self.assertEqual(tray_application.status_action.text(), "")
        tray_application.shutdown()

    def test_notifications_are_bounded_private_and_open_recovery_surface(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        private_error = "/Users/player/private/story.txt: " + "failure " * 80
        with (
            patch.object(tray_application.tray, "showMessage") as notification,
            patch.object(tray_application, "open_support_center") as support,
        ):
            tray_application.show_error(private_error)
            title, body, _icon = notification.call_args.args
            self.assertLessEqual(len(title), 64)
            self.assertLessEqual(len(body), 180)
            self.assertNotIn("/Users/player", body)
            self.assertIn("Support and logs", body)
            tray_application.tray.messageClicked.emit()

        support.assert_called_once_with()
        self.assertIn(private_error, tray_application.status_action.toolTip())
        tray_application.shutdown()

    def test_background_notification_activation_opens_full_controls(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        with (
            patch.object(tray_application.tray, "showMessage") as notification,
            patch.object(tray_application, "show_dashboard") as full_controls,
        ):
            tray_application.notify_background_mode()
            tray_application.notify_background_mode()
            tray_application.tray.messageClicked.emit()

        notification.assert_called_once()
        full_controls.assert_called_once_with()
        tray_application.shutdown()

    def test_external_worker_exit_codes_are_validated_before_qt_startup(self):
        workers = (
            ("bootstrap", ["--game-content-import-worker", "reverse1999"]),
            ("source_audio_duration", ["--source-audio-publisher-worker", "duration"]),
            (
                "source_audio_semantics",
                ["--source-audio-publisher-worker", "semantics"],
            ),
            ("live_sequence", ["--prepared-sequence-worker"]),
        )
        for module_name, arguments in workers:
            package = ModuleType("r1999extractor")
            package.__path__ = []
            module = ModuleType(f"r1999extractor.{module_name}")
            worker = Mock()
            module.main = worker
            with (
                patch.dict(
                    "sys.modules",
                    {"r1999extractor": package, module.__name__: module},
                ),
                patch("vntts.app.QApplication") as qt_application,
            ):
                for value in (None, True, "0", object()):
                    with self.subTest(module=module_name, value=value):
                        worker.return_value = value
                        with self.assertRaisesRegex(TypeError, "exit code"):
                            main(arguments)
                for code in (0, 1, 7, 127):
                    with self.subTest(module=module_name, code=code):
                        worker.return_value = code
                        self.assertEqual(main(arguments), code)
                worker.assert_called_with([])
                qt_application.assert_not_called()

    def test_packaged_content_import_worker_runs_without_creating_qt(self):
        package = ModuleType("r1999extractor")
        package.__path__ = []
        bootstrap_module = ModuleType("r1999extractor.bootstrap")
        bootstrap = Mock(return_value=0)
        bootstrap_module.main = bootstrap
        with (
            patch.dict(
                "sys.modules",
                {
                    "r1999extractor": package,
                    "r1999extractor.bootstrap": bootstrap_module,
                },
            ),
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--game-content-import-worker",
                    "reverse1999",
                    "--data-directory",
                    "/tmp/import-output",
                ]
            )

        self.assertEqual(result, 0)
        bootstrap.assert_called_once_with(["--data-directory", "/tmp/import-output"])
        qt_application.assert_not_called()

    def test_packaged_source_audio_semantic_worker_runs_without_creating_qt(self):
        package = ModuleType("r1999extractor")
        package.__path__ = []
        semantics_module = ModuleType("r1999extractor.source_audio_semantics")
        semantics = Mock(return_value=0)
        semantics_module.main = semantics
        with (
            patch.dict(
                "sys.modules",
                {
                    "r1999extractor": package,
                    "r1999extractor.source_audio_semantics": semantics_module,
                },
            ),
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--source-audio-publisher-worker",
                    "semantics",
                    "--story-index",
                    "/tmp/timed-story.jsonl",
                ]
            )

        self.assertEqual(result, 0)
        semantics.assert_called_once_with(["--story-index", "/tmp/timed-story.jsonl"])
        qt_application.assert_not_called()

    def test_packaged_prepared_sequence_worker_runs_without_creating_qt(self):
        package = ModuleType("r1999extractor")
        package.__path__ = []
        sequence_module = ModuleType("r1999extractor.live_sequence")
        sequence = Mock(return_value=0)
        sequence_module.main = sequence
        with (
            patch.dict(
                "sys.modules",
                {
                    "r1999extractor": package,
                    "r1999extractor.live_sequence": sequence_module,
                },
            ),
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--prepared-sequence-worker",
                    "--story-index",
                    "/tmp/story-index.jsonl",
                ]
            )

        self.assertEqual(result, 0)
        sequence.assert_called_once_with(["--story-index", "/tmp/story-index.jsonl"])
        qt_application.assert_not_called()

    def test_packaged_generation_worker_runs_without_creating_qt(self):
        with (
            patch(
                "vntts.authoring.cli_generation.main", return_value=0
            ) as generation_main,
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--offline-generation-worker",
                    "generate",
                    "--queue",
                    "/tmp/queue.jsonl",
                ]
            )

        self.assertEqual(result, 0)
        generation_main.assert_called_once_with(
            ["generate", "--queue", "/tmp/queue.jsonl"]
        )
        qt_application.assert_not_called()

    def test_offline_audio_action_opens_guided_selection_and_reports_saved_scope(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        job = Mock()
        job.estimate.selected_lines = 42
        voice_plan = Mock()
        voice_plan.groups = (Mock(), Mock(), Mock())
        voice_plan.narrator_fallback_count = 1
        generation_input = Mock()
        generation_input.ready_items = 39
        generation_result = Mock()
        generation_result.generated = 38
        generation_result.failed = 1
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.has_pending_work.return_value = False
        dialog.job.return_value = job
        dialog.voice_plan.return_value = voice_plan
        dialog.generation_input.return_value = generation_input
        dialog.generation_result.return_value = generation_result
        pack_result = OfflinePackResult(
            identity="a" * 64,
            directory=Path("/tmp/offline-pack"),
            manifest=Path("/tmp/offline-pack/game-pack.json"),
            imported=Mock(),
            approved=38,
            live_fallbacks=1,
            story_lines=42,
        )
        dialog.pack_result.return_value = pack_result

        with (
            patch(
                "vntts.app.OfflineAudioPreparationDialog",
                return_value=dialog,
            ) as create_dialog,
            patch.object(
                tray_application,
                "_start_pregeneration_activation",
            ) as start_activation,
            patch.object(tray_application.dashboard, "embed_preparation"),
            patch.object(tray_application.dashboard, "remove_preparation"),
        ):
            result = tray_application.open_pregeneration()
            tray_application._pregeneration_finished(QDialog.DialogCode.Accepted)

        self.assertIs(result, dialog)
        create_dialog.assert_called_once_with(
            tray_application.settings,
            audition_service=ANY,
            generator=ANY,
            game_narrator_chooser=tray_application._open_preparation_narrator,
            automatic_activation=True,
            parent=tray_application.dashboard,
        )
        start_activation.assert_called_once()
        self.assertIs(start_activation.call_args.args[0], pack_result)
        status = start_activation.call_args.args[1]
        self.assertIn("covers 42 dialogue lines", status)
        self.assertIn("Matched 3 voice groups", status)
        self.assertIn("1 will use narrator", status)
        self.assertIn("38 have prepared voices", status)
        self.assertIn("1 will use live voice", status)
        tray_application.shutdown()

    def test_valid_saved_pack_activates_without_generation_transients(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        dialog = Mock()
        dialog.job.return_value = None
        dialog.voice_plan.return_value = None
        dialog.settings = tray_application.settings
        pack_result = OfflinePackResult(
            identity="b" * 64,
            directory=Path("/tmp/saved-offline-pack"),
            manifest=Path("/tmp/saved-offline-pack/game-pack.json"),
            imported=Mock(),
            approved=12,
            live_fallbacks=0,
            story_lines=12,
        )
        dialog.pack_result.return_value = pack_result
        tray_application.pregeneration_dialog = dialog

        with (
            patch.object(tray_application.dashboard, "remove_preparation"),
            patch.object(
                tray_application, "_start_pregeneration_activation"
            ) as start_activation,
        ):
            result = tray_application._pregeneration_finished(
                QDialog.DialogCode.Accepted
            )

        self.assertIsNone(result)
        start_activation.assert_called_once()
        self.assertIs(start_activation.call_args.args[0], pack_result)
        self.assertIn("covers 12 dialogue lines", start_activation.call_args.args[1])
        dialog.generation_input.assert_not_called()
        dialog.generation_result.assert_not_called()
        tray_application.shutdown()

    def test_automatic_pack_activation_requires_unchanged_idle_context(self):
        controller = Mock(is_ready=False, is_live_running=False)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        panel = Mock()
        tray.pregeneration_dialog = panel
        tray._remember_preparation_context()
        for state in ("unchanged", "changed", "reading", "busy", "voice", "quitting"):
            with self.subTest(state=state):
                panel.reset_mock()
                tray.settings = (
                    AppSettings().updated(tts_speaker="marius")
                    if state == "changed"
                    else AppSettings()
                )
                controller.is_live_running = state == "reading"
                tray._controller_busy = state == "busy"
                tray.narrator_dialog = Mock() if state == "voice" else None
                tray._quit_requested = state == "quitting"
                tray._activate_ready_preparation()
                if state == "unchanged":
                    panel.accept.assert_called_once()
                else:
                    panel.accept.assert_not_called()
                    if state != "quitting":
                        panel.defer_activation.assert_called_once()
        tray.pregeneration_dialog = None
        tray.narrator_dialog = None
        tray.shutdown()

    def test_stopping_partial_reading_activates_the_completed_pack(self):
        controller = Mock(is_ready=True, is_live_running=False)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        panel = Mock()
        panel.pack_result.return_value = OfflinePackResult(
            identity="b" * 64,
            directory=Path("/tmp/saved-offline-pack"),
            manifest=Path("/tmp/saved-offline-pack/game-pack.json"),
            imported=Mock(),
            approved=12,
            live_fallbacks=0,
            story_lines=12,
        )
        tray.pregeneration_dialog = panel

        with patch.object(tray, "_activate_ready_preparation") as activate:
            tray.set_live(False)
            self.wait_until(lambda: activate.called)

        activate.assert_called_once_with()
        tray.pregeneration_dialog = None
        tray.shutdown()

    def test_main_narrator_entry_uses_live_settings_with_preparation_open(self):
        controller = Mock(is_ready=False, is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.has_pending_work.return_value = False
        dialog.settings = AppSettings(offline_speech_backend="moss-tts")
        tray_application.pregeneration_dialog = dialog
        with (
            patch("vntts.app.GameNarratorDialog") as factory,
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
        ):
            tray_application.open_voice_previews()
            factory.assert_called_once_with(
                tray_application.settings,
                tray_application.dashboard,
                use_offline_engine=False,
            )
            tray_application._narrator_finished(QDialog.DialogCode.Rejected)
        controller.start.assert_not_called()
        tray_application.pregeneration_dialog = None
        tray_application.shutdown()

    def test_preparation_narrator_entry_uses_offline_settings(self):
        controller = Mock(is_ready=False, is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        preparation = Mock()
        preparation.has_pending_work.return_value = False
        preparation.settings = AppSettings(offline_speech_backend="moss-tts")
        tray_application.pregeneration_dialog = preparation
        with (
            patch("vntts.app.GameNarratorDialog") as factory,
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
        ):
            tray_application._open_preparation_narrator(
                preparation.settings, tray_application.dashboard
            )
            factory.assert_called_once_with(
                resolve_pregeneration_settings(preparation.settings),
                tray_application.dashboard,
                use_offline_engine=True,
            )
            tray_application._narrator_finished(QDialog.DialogCode.Rejected)
        tray_application.pregeneration_dialog = None
        tray_application.shutdown()

    def test_preparation_voice_save_preserves_live_reference_and_consent(self):
        settings = AppSettings(
            speech_backend="pocket-tts",
            offline_speech_backend="moss-tts",
            tts_speaker_wav="live.wav",
            pocket_gated_model_accepted=True,
        )
        tray_application = TrayApplication(
            self.application,
            settings,
            controller_factory=Mock(return_value=Mock()),
        )
        dialog = Mock(
            result_settings=settings.updated(
                speech_backend="moss-tts",
                tts_speaker_wav=None,
                pocket_gated_model_accepted=False,
            )
        )

        with patch.object(tray_application, "_save_settings_candidate"):
            saved = tray_application._save_narrator_candidate(dialog, offline=True)

        self.assertEqual(saved.speech_backend, "pocket-tts")
        self.assertEqual(saved.tts_speaker_wav, "live.wav")
        self.assertTrue(saved.pocket_gated_model_accepted)
        tray_application.shutdown()

    def test_shared_voice_entry_and_reading_start_use_existing_actions(self):
        controller = Mock(is_ready=False, is_live_running=False)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        with patch.object(tray, "open_voice_previews") as voices:
            tray.open_speaker_mapping()
        voices.assert_called_once()
        with (
            patch.object(tray, "_save_settings_candidate"),
            patch.object(tray, "prepare_reading") as prepare,
            patch.object(tray, "toggle_live") as start_reading,
        ):
            tray._read_prepared_story()
            prepare.assert_called_once()
            start_reading.assert_not_called()
            tray._controller_ready = True
            tray._read_prepared_story()
            start_reading.assert_called_once()
            controller.is_live_running = True
            tray._read_prepared_story()
            start_reading.assert_called_once()
        controller.start.assert_not_called()
        tray.shutdown()

    def test_ready_dialogue_starts_with_runtime_progress_settings(self):
        saved = AppSettings(onboarding_completed=True)
        progress = saved.updated(
            story_index="selected-story.jsonl",
            generated_audio_manifest="live-progress-manifest.json",
            audio_source_policy="prefer-game-audio",
        )
        backend = Mock()
        controller = Mock(
            is_ready=True,
            is_live_running=False,
            settings=saved,
            speech_backend=backend,
        )
        controller.apply_settings.return_value = True
        tray = TrayApplication(
            self.application,
            saved,
            controller_factory=Mock(return_value=controller),
        )
        preparation = Mock()
        preparation.runtime_playback_settings.return_value = progress
        preparation.has_pending_work.return_value = True
        tray.pregeneration_dialog = preparation

        with patch.object(tray, "toggle_live") as start_reading:
            tray._read_prepared_story()

        controller.apply_settings.assert_called_once_with(progress, cancellation=ANY)
        self.assertIs(
            backend.progress_wait_request,
            preparation.prioritize_line,
        )
        self.assertIs(
            backend.progress_line_observed,
            preparation.readingLineObserved.emit,
        )
        start_reading.assert_called_once_with()
        self.assertEqual(tray.settings, saved)
        tray.pregeneration_dialog = None
        tray.shutdown()

    def test_navigation_preserves_pending_preparation_without_global_progress(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(
                return_value=Mock(is_ready=False, is_live_running=False)
            ),
        )
        preparation = Mock()
        preparation.has_pending_work.return_value = True
        tray.pregeneration_dialog = preparation
        tray.dashboard.show()
        tray._preparation_activity_changed(True)
        with patch.object(tray, "_save_settings_candidate"):
            tray.dashboard.show_voices()
            tray.dashboard.show_reading()
        preparation._cancel_or_reject.assert_not_called()
        preparation.reject.assert_not_called()
        preparation.has_pending_work.return_value = False
        tray._preparation_activity_changed(False)
        preparation._cancel_or_reject.assert_not_called()
        tray.pregeneration_dialog = None
        tray.shutdown()

    def test_live_reading_keeps_background_preparation_controls_available(self):
        controller = Mock(is_ready=True, is_live_running=True)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        preparation = Mock()
        preparation.has_pending_work.return_value = True
        tray.pregeneration_dialog = preparation

        tray._apply_controller_action_state()

        preparation.setEnabled.assert_called_with(True)
        tray.pregeneration_dialog = None
        tray.shutdown()

    def test_capture_controls_remain_available_during_preparation(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(
                return_value=Mock(is_ready=True, is_live_running=False)
            ),
        )
        preparation = Mock()
        preparation.has_pending_work.return_value = True
        tray.pregeneration_dialog = preparation
        tray.set_ready(True)
        tray._preparation_activity_changed(True)

        for control in (
            tray.calibrate_action,
            tray.settings_action,
            tray.dashboard.calibrate_button,
            tray.dashboard.settings_button,
        ):
            self.assertTrue(control.isEnabled())
        self.assertFalse(tray.assets_action.isEnabled())
        self.assertFalse(tray.dashboard.setup_primary_button.isEnabled())

        tray.narrator_dialog = Mock()
        tray._apply_controller_action_state()
        self.assertFalse(tray.calibrate_action.isEnabled())
        self.assertFalse(tray.dashboard.settings_button.isEnabled())
        tray.narrator_dialog = None
        tray._controller_busy = True
        tray._apply_controller_action_state()
        self.assertFalse(tray.settings_action.isEnabled())
        self.assertFalse(tray.dashboard.calibrate_button.isEnabled())
        tray._controller_busy = False
        tray.pregeneration_dialog = None
        tray.shutdown()

    def test_capture_controls_wait_for_live_stop_without_cancelling_preparation(self):
        for action in ("calibrate", "open_settings"):
            with self.subTest(action=action):
                release = Event()
                controller = Mock(is_ready=True, is_live_running=True)

                def stop_live():
                    controller.is_live_running = False
                    return False

                controller.toggle_live.side_effect = stop_live
                controller.live_reader.wait.side_effect = lambda **_kwargs: (
                    release.wait(2)
                )
                tray = TrayApplication(
                    self.application,
                    AppSettings(),
                    controller_factory=Mock(return_value=controller),
                )
                preparation = Mock()
                preparation.has_pending_work.return_value = True
                tray.pregeneration_dialog = preparation
                tray.set_ready(True)
                with (
                    patch.object(tray, "_create_settings_dialog") as settings_dialog,
                    patch.object(tray, "_open_calibration_overlay") as overlay,
                    patch.object(
                        tray,
                        "_capture_calibration_background",
                        return_value=(None, object()),
                    ),
                ):
                    settings_dialog.return_value.exec.return_value = (
                        QDialog.DialogCode.Rejected
                    )
                    getattr(tray, action)()
                    self.assertTrue(tray.live_stop_runner.active)
                    self.assertFalse(tray.calibrate_action.isEnabled())
                    self.assertFalse(tray.dashboard.settings_button.isEnabled())
                    settings_dialog.assert_not_called()
                    overlay.assert_not_called()
                    release.set()
                    if action == "calibrate":
                        self.wait_until(lambda: overlay.called)
                    else:
                        self.wait_until(lambda: settings_dialog.called)
                controller.toggle_live.assert_called_once_with()
                if action == "calibrate":
                    controller.live_reader.wait.assert_called_once_with(
                        timeout_seconds=5.0, include_speech=False
                    )
                else:
                    controller.live_reader.wait.assert_called_once_with(
                        timeout_seconds=5.0
                    )
                preparation._cancel_or_reject.assert_not_called()
                preparation.reject.assert_not_called()
                self.assertIs(tray.pregeneration_dialog, preparation)
                tray.pregeneration_dialog = None
                tray.shutdown()

    def test_narrator_completion_returns_only_to_its_original_preparation(self):
        for result in (QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected):
            for origin in (0, 1, 2):
                for same_preparation in (True, False):
                    with self.subTest(
                        result=result, origin=origin, same_preparation=same_preparation
                    ):
                        original = AppSettings()
                        candidate = original.updated(tts_speaker="marius")
                        tray = TrayApplication(
                            self.application,
                            original,
                            controller_factory=Mock(
                                return_value=Mock(is_ready=False, is_live_running=False)
                            ),
                        )
                        preparation = Mock(settings=original)
                        preparation.has_pending_work.return_value = False
                        tray.pregeneration_dialog = preparation
                        narrator = Mock(result_settings=candidate)
                        saved_settings = []
                        with (
                            patch(
                                "vntts.app.GameNarratorDialog", return_value=narrator
                            ),
                            patch.object(tray.dashboard, "embed_narrator"),
                            patch.object(tray.dashboard, "remove_narrator"),
                            patch.object(
                                tray,
                                "_save_settings_candidate",
                                side_effect=lambda settings: (
                                    saved_settings.append(settings)
                                    or Path("settings.json")
                                ),
                            ),
                            patch.object(tray, "_reload_game_narrator") as reload,
                        ):
                            tray.dashboard.sections.setCurrentIndex(origin)
                            tray.open_voice_previews()
                            tray.dashboard.show_reading()
                            if not same_preparation:
                                tray.pregeneration_dialog = None
                            # Opening Voices refreshes already-saved settings first.
                            preparation.apply_narrator_settings.reset_mock()
                            tray._narrator_finished(result)
                        self.assertEqual(
                            tray.dashboard.sections.currentIndex(),
                            0 if origin == 0 and same_preparation else 2,
                        )
                        if result == QDialog.DialogCode.Accepted:
                            self.assertTrue(
                                any(
                                    settings.tts_speaker == "marius"
                                    for settings in saved_settings
                                )
                            )
                            reload.assert_called_once_with(True)
                            if same_preparation:
                                preparation.apply_narrator_settings.assert_called_once_with(
                                    candidate.updated(last_main_section="reading"),
                                    voice_changed=True,
                                )
                        else:
                            self.assertTrue(
                                all(
                                    settings.tts_speaker is None
                                    for settings in saved_settings
                                )
                            )
                            reload.assert_not_called()
                            preparation.apply_narrator_settings.assert_not_called()
                        self.assertEqual(
                            tray.settings,
                            (
                                candidate
                                if result == QDialog.DialogCode.Accepted
                                else original
                            ).updated(
                                last_main_section="stories"
                                if origin == 0 and same_preparation
                                else "reading"
                            ),
                        )
                        tray.pregeneration_dialog = None
                        tray.shutdown()

    def test_narrator_save_retains_profile_failure_after_runtime_apply(self):
        original = AppSettings(active_profile_id="game")
        candidate = original.updated(tts_speaker="marius")
        controller = Mock(is_ready=False, is_live_running=False)
        tray = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        tray.narrator_dialog = Mock(result_settings=candidate)
        with (
            patch.object(tray.dashboard, "remove_narrator"),
            patch("vntts.app.TrayApplication._save_settings_candidate"),
            patch.object(tray.profile_store, "get", return_value=Mock()),
            patch.object(
                tray.profile_store,
                "update_from_settings",
                side_effect=OSError("disk full"),
            ),
        ):
            tray._narrator_finished(QDialog.DialogCode.Accepted)
            self.wait_until(lambda: not tray._controller_busy)
        self.assertEqual(tray.settings, candidate)
        controller.apply_settings.assert_called_once_with(candidate, cancellation=ANY)
        self.assertIn("Voices saved", tray.status_action.toolTip())
        self.assertIn(
            "Active profile could not be updated", tray.status_action.toolTip()
        )
        tray.shutdown()

    def test_sequence_resync_action_selects_the_visible_canonical_event(self):
        controller = Mock()
        controller.live_sequence_anchor_options.return_value = (
            ("Chapter 1, sequence 1 - Ada: First [event-1]", "event-1"),
            ("Chapter 1, sequence 2 - Bea: Second [event-2]", "event-2"),
        )
        controller.story_cursor.current_event_id = "event-2"
        controller.get_live_sequence_status.return_value = LiveSequenceStatus(
            "audio-manual", "locked", event_id="event-2"
        )
        controller.resync_live_sequence.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.QInputDialog.getItem",
            return_value=(
                "Chapter 1, sequence 1 - Ada: First [event-1]",
                True,
            ),
        ) as choose:
            self.assertTrue(tray_application.choose_sequence_position())

        self.assertTrue(tray_application.sequence_resync_action.isVisible())
        self.assertEqual(choose.call_args.args[4], 1)
        controller.resync_live_sequence.assert_called_once_with("event-1")
        tray_application.shutdown()

    def test_single_expected_sequence_action_uses_current_candidate_without_dialog(
        self,
    ):
        controller = Mock()
        controller.live_sequence_expected_options.return_value = (
            ("Sequence 2 - Ada: Repeated [event-2]", "event-2"),
        )
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.QInputDialog.getItem") as choose:
            self.assertTrue(tray_application.choose_expected_sequence_event())

        choose.assert_not_called()
        controller.select_expected_live_sequence_event.assert_called_once_with(
            "event-2"
        )
        tray_application.shutdown()

    def test_multiple_expected_sequence_candidates_use_bounded_chooser(self):
        controller = Mock()
        options = (
            ("Sequence 2 - Ada: Left [left]", "left"),
            ("Sequence 3 - Bea: Right [right]", "right"),
        )
        controller.live_sequence_expected_options.return_value = options
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.QInputDialog.getItem",
            return_value=(options[1][0], True),
        ):
            self.assertTrue(tray_application.choose_expected_sequence_event())

        controller.select_expected_live_sequence_event.assert_called_once_with("right")
        tray_application.shutdown()

    def test_compact_expected_sequence_button_uses_fresh_controller_candidate(self):
        controller = Mock()
        controller.live_sequence_expected_options.return_value = (
            ("Sequence 2 - Ada: Repeated [event-2]", "event-2"),
        )
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )
        tray_application._controller_ready = True
        tray_application.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "locked",
                expected_candidate_count=1,
            )
        )
        tray_application.compact_controller.set_ready(True)

        tray_application.compact_controller.sequence_expected_button.click()

        controller.live_sequence_expected_options.assert_called_once_with()
        controller.select_expected_live_sequence_event.assert_called_once_with(
            "event-2"
        )
        tray_application.shutdown()

    def test_compact_controls_replace_dashboard_and_persist_preference(self):
        controller = Mock()
        controller.get_capture_geometry.return_value = WindowGeometry(
            100, 200, 1600, 900
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.TrayApplication._save_settings_candidate") as save:
            tray_application.show_compact_controls()
            self.application.processEvents()

        self.assertFalse(tray_application.dashboard.isVisible())
        self.assertTrue(tray_application.compact_controller.isVisible())
        self.assertTrue(tray_application.settings.compact_controls)
        save.assert_called_once_with(tray_application.settings)

        with patch("vntts.app.TrayApplication._save_settings_candidate") as save:
            tray_application.show_dashboard()

        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertFalse(tray_application.compact_controller.isVisible())
        self.assertFalse(tray_application.settings.compact_controls)
        save.assert_called_once_with(tray_application.settings)
        tray_application.shutdown()

    def test_live_status_is_mirrored_from_tray_to_compact_window(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.set_live(True)

        tray_application.set_status(
            "Auto advance paused: source-audio completion is unavailable"
        )

        self.assertEqual(tray_application.compact_controller.mode.text(), "Reading")
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Auto advance paused: source-audio completion is unavailable",
        )
        self.assertEqual(
            tray_application.status_action.text(),
            tray_application.compact_controller.status.text(),
        )
        tray_application.shutdown()

    def test_audio_route_trace_goes_to_support_log_without_replacing_status(self):
        controller_factory = Mock(return_value=Mock())
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )
        tray_application.set_status("Live reading active")
        trace_handler = controller_factory.call_args.kwargs["route_trace_handler"]

        trace_handler(
            AudioRouteTrace(
                3,
                "moss-tts:fresh-generation",
                "exact",
                "generated-audio-entry-not-found",
                "voice:rhiannon-v2:reference-1",
                "reverse1999:3",
                "generated-audio-entry-not-found",
            )
        )

        event = tray_application.support_log.snapshot()[-1]
        self.assertEqual(event["level"], "audio-route")
        self.assertEqual(event["generation"], 3)
        self.assertEqual(event["line_id"], "reverse1999:3")
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Live reading active",
        )
        tray_application.shutdown()

    def test_unknown_speaker_prompt_suppresses_duplicate_notification(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch.object(tray_application.tray, "showMessage") as notification,
            patch("vntts.app.configure_floating_window") as configure_window,
            patch("vntts.app.sys.platform", "darwin"),
        ):
            tray_application.signals.unknown_speaker.emit("Selone")
            self.application.processEvents()

        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.speaker_mapping_action.text(), "Manage voice for Selone..."
        )
        notification.assert_not_called()
        self.assertIn("No voice is assigned", tray_application.dashboard.status.text())
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Voice needed: Selone",
        )
        self.assertIsInstance(
            tray_application.unknown_speaker_prompt,
            QMessageBox,
        )
        self.assertIsNone(tray_application.unknown_speaker_prompt.parent())
        self.assertIn(
            "Selone",
            tray_application.unknown_speaker_prompt.text(),
        )
        self.assertIn(
            "Live reading is waiting",
            tray_application.unknown_speaker_prompt.informativeText(),
        )
        self.assertTrue(
            tray_application.unknown_speaker_prompt.testAttribute(
                Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow
            )
        )
        configure_window.assert_called_once_with(
            tray_application.unknown_speaker_prompt
        )
        self.assertEqual(
            tray_application.unknown_speaker_continue_button.text(),
            "Use narrator this session",
        )
        self.assertEqual(
            tray_application.unknown_speaker_choose_button.text(),
            "Choose and save voice...",
        )
        self.assertEqual(
            tray_application.unknown_speaker_cancel_button.text(),
            "Keep reading paused",
        )
        self.assertEqual(
            tray_application.unknown_speaker_prompt.buttonRole(
                tray_application.unknown_speaker_choose_button
            ),
            QMessageBox.ButtonRole.AcceptRole,
        )
        self.assertIs(
            tray_application.unknown_speaker_prompt.defaultButton(),
            tray_application.unknown_speaker_choose_button,
        )
        self.assertIs(
            tray_application.unknown_speaker_prompt.escapeButton(),
            tray_application.unknown_speaker_cancel_button,
        )
        tray_application.unknown_speaker_continue_button.click()
        self.application.processEvents()
        controller.allow_narrator_fallback.assert_called_once_with("Selone")
        tray_application.shutdown()

    def test_unknown_speaker_during_live_reading_is_nonmodal(self):
        controller = Mock(is_live_running=True)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        tray_application.offer_speaker_mapping("Selone")

        self.assertIsNone(tray_application.unknown_speaker_prompt)
        self.assertEqual(tray_application.pending_unknown_speaker, "Selone")
        self.assertIn("Using the narrator", tray_application.status_action.text())
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_pending_speaker_mapping_opens_canonical_editor_on_character(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.pending_unknown_speaker = "Selone"
        dialog = Mock()

        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog) as factory,
            patch.object(tray_application.dashboard, "embed_narrator"),
        ):
            tray_application.open_speaker_mapping()

        factory.assert_called_once_with(
            tray_application.settings,
            tray_application.dashboard,
            use_offline_engine=False,
        )
        dialog.set_voice_context.assert_called_once_with(character="Selone")
        dialog.set_recovery_context.assert_called_once_with("Selone", resume_live=False)
        self.assertEqual(tray_application.pending_unknown_speaker, "Selone")
        self.assertEqual(tray_application.unknown_speaker_mapping_in_progress, "Selone")
        tray_application.narrator_dialog = None
        tray_application.shutdown()

    def test_live_start_does_not_block_on_unassigned_named_speakers(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = ("Selone", "Hotelier")
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        self.assertTrue(tray_application.toggle_live())

        controller.approve_live_narrator_fallbacks.assert_not_called()
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_live_preflight_identifies_current_scope_silently_then_starts(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [None, ()]
        controller.identify_live_scope.return_value = True
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())

        runner.start.assert_called_once_with(controller.identify_live_scope)
        controller.toggle_live.assert_not_called()

        tray_application._live_scope_finished(True, None)

        controller.toggle_live.assert_called_once_with()
        controller.read_once.assert_not_called()
        tray_application.shutdown()

    def test_live_scope_identification_failure_does_not_start(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(False, None)

        controller.toggle_live.assert_not_called()
        self.assertIn("complete dialog line", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_failure_explains_story_match_instead_of_missing_text(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        controller.live_scope_identification_failure = "story-line-no-match"
        controller.live_scope_identification_match_result = "expected-no-match"
        controller.live_scope_identification_diagnostics = {
            "eligible_line_count": 42,
            "best_bounded_similarity": 0.73,
        }
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application.dashboard.show_reading()
        with patch("vntts.app.QMessageBox") as message_box:
            continue_button, stories_button, cancel_button = (
                object(),
                object(),
                object(),
            )
            prompt = message_box.return_value
            prompt.addButton.side_effect = (
                continue_button,
                stories_button,
                cancel_button,
            )
            prompt.clickedButton.return_value = stories_button
            tray_application._live_scope_finished(False, None)

        self.assertIn("Stories is open", tray_application.dashboard.status.text())
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertEqual(tray_application.dashboard.sections.currentIndex(), 0)
        self.assertNotIn("not visible", tray_application.dashboard.status.text())
        self.assertIn(
            "expected-no-match",
            "\n".join(
                entry["message"] for entry in tray_application.support_log.snapshot()
            ),
        )
        event = next(
            entry
            for entry in reversed(tray_application.support_log.snapshot())
            if entry["level"] == "live-scope"
        )
        self.assertEqual(event["eligible_line_count"], 42)
        self.assertEqual(event["best_bounded_similarity"], 0.73)
        tray_application.shutdown()

    def test_live_scope_failure_can_continue_from_ocr_without_changing_pack(self):
        controller = Mock(is_live_running=False)
        controller.start_live_from_ocr.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                story_index="story.jsonl",
                generated_audio_manifest="generated.json",
            ),
            controller_factory=Mock(return_value=controller),
        )
        with patch("vntts.app.QMessageBox") as message_box:
            continue_button, stories_button, cancel_button = (
                object(),
                object(),
                object(),
            )
            prompt = message_box.return_value
            prompt.addButton.side_effect = (
                continue_button,
                stories_button,
                cancel_button,
            )
            prompt.clickedButton.return_value = continue_button

            self.assertTrue(tray_application._offer_story_match_recovery("No match"))

        controller.start_live_from_ocr.assert_called_once_with()
        self.assertEqual(tray_application.dashboard.sections.currentIndex(), 2)
        self.assertEqual(tray_application.settings.story_index, "story.jsonl")
        self.assertEqual(
            tray_application.settings.generated_audio_manifest,
            "generated.json",
        )
        self.assertIn(
            "Prepared recordings remain available",
            tray_application.dashboard.status.text(),
        )
        tray_application.shutdown()

    def test_story_match_recovery_names_each_outcome_and_escape(self):
        prompt, read, stories, stop = build_story_match_recovery_prompt(
            "The visible dialogue does not match the prepared story at this point."
        )
        self.addCleanup(prompt.deleteLater)
        self.assertIn("does not match", prompt.text())
        self.assertIn("Prepared recordings remain available", prompt.informativeText())
        self.assertEqual(read.text(), "Start live reading")
        self.assertEqual(stories.text(), "Open Stories...")
        self.assertEqual(stop.text(), "Stay stopped")
        self.assertIs(prompt.defaultButton(), read)
        self.assertIs(prompt.escapeButton(), stop)

    def test_story_match_recovery_can_leave_reading_stopped(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        with patch("vntts.app.QMessageBox") as message_box:
            read, stories, stop = object(), object(), object()
            prompt = message_box.return_value
            prompt.addButton.side_effect = (read, stories, stop)
            prompt.clickedButton.return_value = stop

            self.assertFalse(
                tray_application._offer_story_match_recovery(
                    "The visible dialogue does not match the prepared story."
                )
            )

        controller.start_live_from_ocr.assert_not_called()
        self.assertIn("remains stopped", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_failure_explains_empty_capture(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        controller.live_scope_identification_failure = "no-dialog-text"
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(False, None)

        self.assertIn("no dialog text", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_identification_starts_with_unresolved_speakers(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [None, ("Hotelier",)]
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(True, None)

        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_second_reading_toggle_cancels_scope_identification(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())
        self.assertEqual(tray_application.live_action.text(), "Cancel start")
        self.assertFalse(tray_application.toggle_live())

        runner.start.assert_called_once_with(controller.identify_live_scope)
        runner.cancel.assert_called_once_with()
        self.wait_until(lambda: controller.emergency_stop.called)
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_stale_live_scope_identification_cannot_start_live_mode(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._lifecycle_generation += 1
        tray_application._live_scope_finished(True, None)

        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_emergency_stop_cancels_pending_live_scope_identification(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())
        tray_application.emergency_stop()
        tray_application._live_scope_finished(True, None)

        runner.cancel.assert_called_once_with()
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_stop_reading_cancels_live_and_one_time_speech(self):
        controller = Mock(is_live_running=True)
        controller.live_reader.runtime_control_snapshot.return_value = {}
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        self.assertFalse(tray_application.toggle_live())
        self.wait_until(lambda: not tray_application.live_stop_runner.active)
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()

        controller.reset_mock()
        controller.is_live_running = False
        controller.live_reader.runtime_control_snapshot.return_value = {
            "speaking": True,
            "queued": True,
        }
        self.assertFalse(tray_application.toggle_live())
        self.wait_until(lambda: not tray_application.live_stop_runner.active)
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()

        controller.reset_mock()
        controller.live_reader.runtime_control_snapshot.return_value = {}
        controller.is_one_shot_read_running = True
        self.assertEqual(tray_application._runtime_control_state().active, True)
        self.assertFalse(tray_application.toggle_live())
        self.wait_until(lambda: not tray_application.live_stop_runner.active)
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_stop_disables_all_start_stop_controls_until_reader_quiesces(self):
        release = Event()
        controller = Mock(is_ready=True, is_live_running=True)

        def stop_reader():
            controller.is_live_running = False
            return True

        controller.emergency_stop.side_effect = stop_reader
        controller.live_reader.wait.side_effect = lambda **_kwargs: release.wait(2)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray.set_ready(True)

        self.assertFalse(tray.toggle_live())
        self.assertTrue(tray.live_stop_runner.active)
        self.assertEqual(tray.dashboard.live_button.text(), "Stopping reading...")
        for control in (
            tray.live_action,
            tray.dashboard.live_button,
            tray.compact_controller.live_button,
        ):
            self.assertFalse(control.isEnabled())
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        self.application.processEvents()
        self.assertEqual(heartbeat, [True])
        self.assertFalse(tray.toggle_live())
        self.wait_until(lambda: controller.emergency_stop.called)
        controller.emergency_stop.assert_called_once_with()

        release.set()
        self.wait_until(lambda: not tray.live_stop_runner.active)
        self.assertTrue(tray.dashboard.live_button.isEnabled())
        self.assertEqual(tray.dashboard.live_button.text(), "Start reading")
        tray.shutdown()

    def test_voice_mapping_resumes_live_mode_after_assignment(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        dialog = Mock(result_settings=tray_application.settings)
        dialog.voice_library.binding.return_value = Mock()
        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog),
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
            patch.object(tray_application, "_save_settings_candidate"),
            patch.object(tray_application, "_sync_active_profile", return_value=True),
            patch.object(tray_application, "_reload_game_narrator"),
        ):
            tray_application._open_pending_speaker_mapping("Selone")
            self.wait_until(lambda: tray_application.narrator_dialog is dialog)
            self.assertFalse(controller.is_live_running)
            tray_application._narrator_finished(QDialog.DialogCode.Accepted)
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with(timeout_seconds=5.0)
        self.assertFalse(tray_application.resume_live_after_unknown_mapping)
        self.assertIsNone(tray_application.pending_unknown_speaker)
        tray_application.shutdown()

    def test_missing_narrator_result_does_not_save_settings(self):
        tray_application = TrayApplication(self.application, AppSettings())
        with (
            patch.object(tray_application, "_save_settings_candidate") as save,
            patch.object(tray_application, "show_error") as show_error,
        ):
            self.assertIsNone(
                tray_application._save_narrator_candidate(Mock(result_settings=None))
            )
        save.assert_not_called()
        show_error.assert_called_once_with(
            "Voice selection finished without saved settings"
        )
        tray_application.shutdown()

    def test_cancelled_voice_mapping_stays_paused_and_reoffers_choice(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.resume_live_after_unknown_mapping = True
        dialog = Mock()

        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog),
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
            patch.object(tray_application, "_show_unknown_speaker_prompt") as prompt,
        ):
            tray_application._open_pending_speaker_mapping("Selone")
            tray_application._narrator_finished(QDialog.DialogCode.Rejected)
            self.application.processEvents()

        controller.toggle_live.assert_not_called()
        prompt.assert_called_once_with("Selone")
        self.assertEqual(tray_application.pending_unknown_speaker, "Selone")
        self.assertTrue(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_second_unknown_speaker_waits_for_active_mapping(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.resume_live_after_unknown_mapping = True
        dialog = Mock(result_settings=tray_application.settings)
        dialog.voice_library.binding.return_value = Mock()

        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog),
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
            patch.object(tray_application, "_save_settings_candidate"),
            patch.object(tray_application, "_sync_active_profile", return_value=True),
            patch.object(tray_application, "_reload_game_narrator"),
            patch.object(tray_application, "_show_unknown_speaker_prompt") as prompt,
        ):
            tray_application._open_pending_speaker_mapping("Selone")
            tray_application.offer_speaker_mapping("Hotelier")
            self.assertEqual(tray_application.pending_unknown_speaker, "Selone")
            tray_application._narrator_finished(QDialog.DialogCode.Accepted)
            self.application.processEvents()

        controller.toggle_live.assert_not_called()
        prompt.assert_called_once_with("Hotelier")
        self.assertEqual(tray_application.pending_unknown_speaker, "Hotelier")
        self.assertEqual(tray_application._queued_unknown_speakers, [])
        self.assertTrue(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_duplicate_unknown_speaker_is_ignored_while_mapping_is_open(self):
        controller = Mock(is_live_running=False)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.unknown_speaker_mapping_in_progress = "Selone"
        tray_application.pending_unknown_speaker = "Selone"
        tray_application.resume_live_after_unknown_mapping = True

        with patch.object(tray_application, "_show_unknown_speaker_prompt") as prompt:
            tray_application.offer_speaker_mapping("SELONE")

        prompt.assert_not_called()
        self.assertEqual(tray_application.pending_unknown_speaker, "Selone")
        self.assertTrue(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_narrator_choice_resumes_live_after_cancelled_voice_mapping(self):
        controller = Mock()
        controller.is_live_running = False
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = True
            return True

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.resume_live_after_unknown_mapping = True

        tray_application._continue_unknown_with_narrator("Selone")

        controller.allow_narrator_fallback.assert_called_once_with("Selone")
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.toggle_live.assert_called_once_with()
        self.assertTrue(controller.is_live_running)
        self.assertFalse(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_macos_tray_icon_is_a_distinct_adaptive_mask(self):
        icon = create_application_icon(
            self.application.style(),
            platform="darwin",
        )

        self.assertFalse(icon.isNull())
        self.assertTrue(icon.isMask())
        self.assertFalse(icon.pixmap(64, 64).isNull())

    def test_other_platforms_keep_the_native_speaker_icon(self):
        icon = create_application_icon(
            self.application.style(),
            platform="win32",
        )

        self.assertFalse(icon.isNull())
        self.assertFalse(icon.isMask())

    def test_saved_ocr_corrections_are_reloaded_by_controller(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted

        with patch("vntts.app.OCRCorrectionsDialog", return_value=dialog):
            tray_application.open_corrections()

        controller.refresh_corrections.assert_called_once_with()
        self.assertEqual(tray_application.status_action.text(), "OCR corrections saved")
        tray_application.shutdown()

    def test_ocr_review_uses_active_profile_and_runtime_reload(self):
        with TemporaryDirectory() as temporary_directory:
            profile_store = GameProfileStore(
                Path(temporary_directory) / "profiles.json"
            )
            profile = profile_store.create("Game", AppSettings())
            controller = Mock()
            tray_application = TrayApplication(
                self.application,
                AppSettings(
                    active_profile_id=profile.id,
                    ocr_diagnostics_directory="review",
                ),
                controller_factory=Mock(return_value=controller),
                profile_store=profile_store,
            )
            dialog = Mock()

            with patch("vntts.app.OCRReviewDialog", return_value=dialog) as factory:
                tray_application.open_ocr_review()

        factory.assert_called_once_with(
            "review",
            tray_application.correction_store,
            profile.id,
            "Game",
            controller.refresh_corrections,
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_settings_expose_minimum_ocr_confidence(self):
        dialog = SettingsDialog(
            AppSettings(
                ocr_minimum_confidence=73,
                retain_uncertain_frames=True,
                ocr_diagnostics_directory="custom/ocr-diagnostics",
            )
        )

        self.assertEqual(dialog.ocr_minimum_confidence.value(), 73)
        dialog.ocr_minimum_confidence.setValue(81)

        self.assertEqual(dialog.settings().ocr_minimum_confidence, 81)
        self.assertTrue(dialog.settings().retain_uncertain_frames)
        self.assertEqual(
            dialog.settings().ocr_diagnostics_directory,
            "custom/ocr-diagnostics",
        )
        delete_dialog(dialog)

    def test_settings_are_scrollable_and_grouped_into_visual_regions(self):
        dialog = SettingsDialog(AppSettings())

        self.assertTrue(dialog.settings_scroll.widgetResizable())
        restart_note = next(
            label
            for label in dialog.findChildren(QLabel)
            if "reload speech when saved" in label.text()
        )
        self.assertTrue(dialog.settings_scroll.widget().isAncestorOf(restart_note))
        self.assertEqual(
            [region.title() for region in dialog.settings_regions],
            [
                "Keyboard shortcuts",
                "Capture and OCR",
                "Speech and voices",
                "Playback and automation",
                "Application behavior",
            ],
        )
        available = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.width(), max(320, available.width() - 64))
        self.assertLessEqual(dialog.height(), max(320, available.height() - 64))
        self.assertTrue(
            all(
                region.layout().fieldGrowthPolicy()
                == region.layout().FieldGrowthPolicy.AllNonFixedFieldsGrow
                for region in dialog.settings_regions
            )
        )
        self.assertEqual(
            [
                dialog.section_navigation.itemText(index)
                for index in range(dialog.section_navigation.count())
            ],
            [region.title() for region in dialog.settings_regions],
        )
        dialog.show()
        dialog.section_navigation.setFocus()
        QTest.keyClick(dialog.section_navigation, Qt.Key.Key_End)
        self.application.processEvents()
        self.assertEqual(dialog.section_navigation.currentIndex(), 4)
        self.assertEqual(dialog.settings_scroll.verticalScrollBar().value(), 0)
        self.assertEqual(
            [region.isVisibleTo(dialog) for region in dialog.settings_regions],
            [False, False, False, False, True],
        )
        dialog.section_navigation.setCurrentIndex(2)
        self.assertTrue(dialog.speech_backend.isVisibleTo(dialog))
        self.assertEqual(
            dialog.choose_narrator_button.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Fixed,
        )
        self.assertEqual(
            dialog.speech_backend.toolTip(), dialog.speech_backend.currentText()
        )
        dialog.audio_source_policy.setCurrentIndex(2)
        self.assertEqual(
            dialog.audio_source_policy.toolTip(),
            dialog.audio_source_policy.currentText(),
        )
        self.assertFalse(dialog.settings_regions[4].isVisibleTo(dialog))
        self.assertTrue(restart_note.isVisibleTo(dialog))
        dialog.section_navigation.setCurrentIndex(1)
        self.assertFalse(restart_note.isVisibleTo(dialog))
        self.assertLess(
            dialog.refresh_windows_button.width(), dialog.game_window.width()
        )
        self.assertFalse(dialog.refresh_windows_button.isEnabled())
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
        self.assertTrue(dialog.refresh_windows_button.isEnabled())
        dialog.section_navigation.setCurrentIndex(2)
        dialog.resize(620, 500)
        self.application.processEvents()
        viewport = dialog.settings_scroll.viewport()
        self.assertTrue(
            viewport.rect().contains(
                dialog.speech_backend.mapTo(
                    viewport, dialog.speech_backend.rect().topRight()
                )
            )
        )
        delete_dialog(dialog)

    def test_settings_section_switch_keeps_advanced_draft_and_scrolls_to_last_field(
        self,
    ):
        dialog = SettingsDialog(AppSettings())
        dialog.resize(620, 500)
        dialog.show()
        dialog.section_navigation.setCurrentIndex(1)
        dialog.advanced_settings.setChecked(True)
        dialog.ocr_language.setText("jpn")
        dialog.section_navigation.setCurrentIndex(2)
        dialog.section_navigation.setCurrentIndex(1)
        self.assertEqual(dialog.ocr_language.text(), "jpn")
        self.assertTrue(dialog.ocr_language.isVisibleTo(dialog))
        dialog.advanced_settings.setChecked(False)
        self.assertTrue(dialog.ocr_language.isHidden())
        self.assertEqual(dialog.ocr_language.text(), "jpn")
        dialog.advanced_settings.setChecked(True)
        dialog.section_navigation.setCurrentIndex(2)
        self.application.processEvents()
        scrollbar = dialog.settings_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(scrollbar.maximum())
        self.application.processEvents()
        viewport = dialog.settings_scroll.viewport()
        self.assertTrue(
            viewport.rect().contains(
                dialog.tts_profile.mapTo(viewport, dialog.tts_profile.rect().center())
            )
        )
        self.assertTrue(dialog.save_button.isVisibleTo(dialog))
        delete_dialog(dialog)

    def test_settings_paths_share_browse_and_accessibility_contract(self):
        dialog = SettingsDialog(AppSettings())
        fields_and_buttons = (
            (dialog.screenshot_directory, dialog.screenshot_browse_button),
            (dialog.ocr_diagnostics_directory, dialog.diagnostics_browse_button),
            (dialog.narrator_reference, dialog.narrator_reference_button),
            (dialog.game_pack, dialog.game_pack_button),
            (dialog.voice_manifest, dialog.voice_manifest_button),
            (dialog.story_index, dialog.story_index_button),
            (dialog.live_sequence_plan, dialog.live_sequence_plan_button),
            (dialog.live_speaker_corpus, dialog.live_speaker_corpus_button),
            (
                dialog.generated_audio_manifest,
                dialog.generated_audio_manifest_button,
            ),
        )

        for field, button in fields_and_buttons:
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.accessibleDescription())

        with TemporaryDirectory() as temporary_directory:
            selected = Path(temporary_directory) / "story.jsonl"
            selected.touch()
            with patch(
                "vntts.app.QFileDialog.getOpenFileName",
                return_value=(str(selected), ""),
            ):
                dialog.story_index_button.click()
            self.assertEqual(dialog.story_index.text(), str(selected))
        delete_dialog(dialog)

    def test_settings_inline_validation_lists_all_errors_and_focuses_first(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            narrator = root / "narrator.wav"
            narrator.touch()
            dialog = SettingsDialog(
                AppSettings(screenshot_directory=str(root)),
                voice_library=VoiceLibrary(root / "voices"),
            )
            dialog.screenshot_directory.clear()
            dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
            dialog.game_window.setCurrentText("")
            dialog.speech_backend.setCurrentIndex(
                dialog.speech_backend.findData("moss-tts")
            )
            dialog.narrator_reference.clear()
            dialog.show()
            self.application.processEvents()

            dialog.validate_and_accept()
            self.application.processEvents()

            self.assertNotEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            self.assertIn("Screenshot directory", dialog.validation_summary.text())
            self.assertIn("Capture source", dialog.validation_summary.text())
            self.assertIn("Narrator voice", dialog.validation_summary.text())
            self.assertEqual(dialog.section_navigation.currentIndex(), 1)
            self.assertTrue(dialog.screenshot_directory.hasFocus())

            dialog.screenshot_directory.setText(str(root))
            dialog.game_window.setCurrentText("Reverse: 1999")
            dialog.narrator_reference.setText(str(narrator))
            self.assertEqual(
                dialog.validation_summary.text(), "All settings are valid."
            )
            dialog.validate_and_accept()
            self.assertEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            delete_dialog(dialog)

    def test_settings_invalid_game_pack_stays_open_with_focused_inline_error(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game_pack = root / "game-pack.json"
            game_pack.write_text("{}", encoding="utf-8")
            dialog = SettingsDialog(AppSettings(screenshot_directory=str(root)))
            dialog.game_pack.setText(str(game_pack))
            dialog.show()
            self.application.processEvents()

            dialog.validate_and_accept()
            self.application.processEvents()

            self.assertNotEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            self.assertIn("Game pack:", dialog.validation_summary.text())
            self.assertEqual(dialog.section_navigation.currentIndex(), 2)
            self.assertTrue(dialog.game_pack.hasFocus())
            delete_dialog(dialog)

    def test_settings_new_game_pack_owns_all_derived_paths(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game_pack = root / "game-pack.json"
            voice_manifest = root / "pack-voices.json"
            story_index = root / "pack-story.jsonl"
            generated_audio = root / "pack-generated.json"
            for path in (voice_manifest, story_index, generated_audio):
                path.touch()

            def apply(settings, path=None):
                self.assertEqual(path, str(game_pack))
                return settings.updated(
                    game_pack=str(game_pack),
                    voice_manifest=str(voice_manifest),
                    story_index=str(story_index),
                    live_sequence_plan=None,
                    live_sequence_mode="off",
                    generated_audio_manifest=str(generated_audio),
                )

            dialog = SettingsDialog(AppSettings(screenshot_directory=str(root)))
            dialog.voice_manifest.setText("hidden-stale-voices.json")
            dialog.story_index.setText("hidden-stale-story.jsonl")
            dialog.live_sequence_plan.setText("hidden-stale-sequence.json")
            dialog.generated_audio_manifest.setText("hidden-stale-generated.json")
            with patch("vntts.app.apply_game_pack", side_effect=apply) as apply_mock:
                dialog.game_pack.setText(str(game_pack))
                dialog.validate_and_accept()
                dialog.output_volume.setValue(45)
                saved = dialog.settings()

            self.assertEqual(apply_mock.call_count, 1)
            self.assertEqual(saved.output_volume_percent, 45)
            self.assertEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            self.assertEqual(saved.voice_manifest, str(voice_manifest))
            self.assertEqual(saved.story_index, str(story_index))
            self.assertIsNone(saved.live_sequence_plan)
            self.assertEqual(saved.live_sequence_mode, "off")
            self.assertEqual(saved.generated_audio_manifest, str(generated_audio))
            delete_dialog(dialog)

    def test_settings_existing_pack_preserves_external_sequence_override(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game_pack = root / "game-pack.json"
            external_sequence = root / "external-sequence.json"
            external_sequence.touch()

            def apply(settings, path=None):
                self.assertIsNone(path)
                return settings.updated(
                    voice_manifest=None,
                    story_index=None,
                    generated_audio_manifest=None,
                )

            original = AppSettings(
                screenshot_directory=str(root),
                game_pack=str(game_pack),
                live_sequence_plan=str(external_sequence),
                live_sequence_mode="shadow",
            )
            with patch("vntts.app.apply_game_pack", side_effect=apply):
                dialog = SettingsDialog(original)
                saved = dialog.settings()

            self.assertEqual(saved.live_sequence_plan, str(external_sequence))
            self.assertEqual(saved.live_sequence_mode, "shadow")
            delete_dialog(dialog)

    def test_settings_allow_nested_screenshot_directory_to_be_created(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "missing" / "screenshots"

            self.assertIsNone(
                SettingsDialog._directory_validation_error(
                    "Screenshot directory", str(directory)
                )
            )

    def test_settings_distinguish_live_and_offline_engines(self):
        dialog = SettingsDialog(
            AppSettings(
                offline_speech_backend="moss-tts",
                offline_tts_model="moss-model.gguf",
            )
        )
        restart_labels = {
            label.text()
            for label in dialog.findChildren(QLabel)
            if label.text().endswith("(restart required)")
        }

        self.assertEqual(
            restart_labels,
            set(),
        )
        self.assertEqual(
            [
                dialog.speech_backend.itemData(i)
                for i in range(dialog.speech_backend.count())
            ],
            [
                dialog.offline_speech_backend.itemData(i)
                for i in range(dialog.offline_speech_backend.count())
            ],
        )
        self.assertEqual(dialog.speech_backend.currentData(), "pocket-tts")
        self.assertEqual(dialog.offline_speech_backend.currentData(), "moss-tts")
        self.assertEqual(dialog.settings().offline_speech_backend, "moss-tts")
        dialog.offline_speech_backend.setCurrentIndex(
            dialog.offline_speech_backend.findData("coqui-xtts")
        )
        self.assertEqual(dialog.offline_tts_model.text(), "")
        self.assertTrue(dialog.tts_language.isEnabled())
        dialog.tts_language.setText("ru")
        self.assertEqual(dialog.settings().tts_language, "ru")
        for field in (
            dialog.speech_backend,
            dialog.tts_model,
            dialog.tts_language,
            dialog.narrator_reference,
            dialog.voice_manifest,
            dialog.narrator_speaker,
        ):
            self.assertIn("reload speech", field.accessibleDescription().casefold())
        delete_dialog(dialog)

    def test_settings_fit_scaled_fonts_with_navigation_and_validation_visible(self):
        base_font = QApplication.font()
        base_size = base_font.pointSizeF() if base_font.pointSizeF() > 0 else 12.0
        fonts = []
        for scale in (1.0, 1.5, 2.0):
            font = QFont(base_font)
            font.setPointSizeF(base_size * scale)
            fonts.append(font)
        # Exercise wide labels even on hosts with compact default font metrics.
        large_font = QFont("Arial")
        large_font.setPixelSize(36)
        fonts.append(large_font)
        for font in fonts:
            with self.subTest(font=font.toString()):
                dialog = SettingsDialog(AppSettings())
                dialog.setFont(font)
                dialog.resize(520, 420)
                dialog.show()
                self.application.processEvents()

                self.assertTrue(dialog.section_navigation.isVisibleTo(dialog))
                self.assertTrue(dialog.validation_summary.isVisibleTo(dialog))
                self.assertTrue(dialog.settings_scroll.isVisibleTo(dialog))
                self.assertTrue(dialog.save_button.isVisibleTo(dialog))

                dialog.section_navigation.setCurrentIndex(2)
                dialog.resize(620, 500)
                self.application.processEvents()
                viewport = dialog.settings_scroll.viewport()
                caption = dialog.speech_form.labelForField(dialog.speech_backend)
                self.assertGreater(caption.width(), 0)
                self.assertGreater(caption.height(), 0)
                self.assertTrue(
                    caption.textInteractionFlags()
                    & Qt.TextInteractionFlag.TextSelectableByKeyboard
                )
                self.assertEqual(
                    dialog.settings_scroll.horizontalScrollBar().maximum(), 0
                )
                self.assertTrue(
                    viewport.rect().contains(
                        dialog.speech_backend.mapTo(
                            viewport, dialog.speech_backend.rect().topRight()
                        )
                    )
                )

                delete_dialog(dialog)

    def test_settings_expose_output_volume_and_speech_rate(self):
        dialog = SettingsDialog(
            AppSettings(output_volume_percent=75, speech_rate_percent=110)
        )

        self.assertEqual(dialog.output_volume.value(), 75)
        self.assertEqual(dialog.speech_rate.value(), 110)
        dialog.output_volume.setValue(45)
        dialog.speech_rate.setValue(125)

        self.assertEqual(dialog.settings().output_volume_percent, 45)
        self.assertEqual(dialog.settings().speech_rate_percent, 125)
        delete_dialog(dialog)

    def test_settings_expose_guarded_auto_advance_controls(self):
        dialog = SettingsDialog(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                auto_advance_enabled=True,
                auto_advance_key="enter",
                auto_advance_delay_ms=600,
            )
        )

        self.assertTrue(dialog.auto_advance.isChecked())
        self.assertEqual(dialog.auto_advance_key.currentData(), "enter")
        self.assertEqual(dialog.auto_advance_delay.value(), 600)
        dialog.auto_advance_key.setCurrentIndex(
            dialog.auto_advance_key.findData("right")
        )
        dialog.auto_advance_delay.setValue(250)

        settings = dialog.settings()
        self.assertEqual(settings.auto_advance_key, "right")
        self.assertEqual(settings.auto_advance_delay_ms, 250)
        delete_dialog(dialog)

    def test_settings_expose_default_fallback_role_announcements(self):
        dialog = SettingsDialog(AppSettings())

        self.assertEqual(
            dialog.speaker_announcement_mode.currentData(),
            "narrator-fallback-roles",
        )
        dialog.speaker_announcement_mode.setCurrentIndex(
            dialog.speaker_announcement_mode.findData("all-speakers")
        )

        self.assertTrue(dialog.settings().announce_speaker_changes)
        self.assertEqual(
            dialog.settings().speaker_announcement_mode,
            "all-speakers",
        )
        delete_dialog(dialog)

    def test_settings_control_startup_voice_warmup(self):
        dialog = SettingsDialog(AppSettings(warm_up_voices=True))

        dialog.warm_up_voices.setChecked(False)

        self.assertFalse(dialog.settings().warm_up_voices)
        delete_dialog(dialog)

    def test_settings_select_low_latency_speech_backend(self):
        dialog = SettingsDialog(AppSettings(speech_backend="chatterbox-nano"))

        self.assertEqual(dialog.speech_backend.currentData(), "chatterbox-nano")
        self.assertFalse(dialog.tts_model.isEnabled())
        self.assertFalse(dialog.tts_language.isEnabled())
        self.assertFalse(dialog.narrator_speaker.isEnabled())
        self.assertFalse(dialog.tts_profile.isEnabled())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertEqual(dialog.settings().speech_backend, "chatterbox-nano")
        delete_dialog(dialog)

    def test_settings_offer_default_streaming_backend(self):
        dialog = SettingsDialog(AppSettings(speech_backend="pocket-tts"))
        dialog.section_navigation.setCurrentIndex(2)

        self.assertEqual(dialog.speech_backend.currentData(), "pocket-tts")
        self.assertIn("recommended", dialog.speech_backend.currentText().casefold())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertTrue(dialog.pocket_gated_model.isVisibleTo(dialog))
        self.assertFalse(dialog.pocket_gated_model.isChecked())
        dialog.pocket_gated_model.setChecked(True)
        self.assertTrue(dialog.settings().pocket_gated_model_accepted)
        delete_dialog(dialog)

    def test_settings_select_explicit_audio_source_policy(self):
        dialog = SettingsDialog(AppSettings(audio_source_policy="prefer-game-audio"))

        self.assertEqual(
            dialog.audio_source_policy.currentData(),
            "prefer-game-audio",
        )
        dialog.audio_source_policy.setCurrentIndex(
            dialog.audio_source_policy.findData("live-tts-only")
        )

        self.assertEqual(dialog.settings().audio_source_policy, "live-tts-only")
        delete_dialog(dialog)

    def test_settings_preserve_explicit_live_speaker_corpus(self):
        dialog = SettingsDialog(
            AppSettings(live_speaker_corpus="session-speakers.json")
        )

        self.assertEqual(dialog.live_speaker_corpus.text(), "session-speakers.json")
        self.assertEqual(
            dialog.settings().live_speaker_corpus,
            "session-speakers.json",
        )
        delete_dialog(dialog)

    def test_settings_sequence_shadow_requires_plan_and_story_index(self):
        dialog = SettingsDialog(AppSettings(live_sequence_mode="shadow"))

        errors = tuple(
            message for _section, _widget, message in dialog.validation_errors()
        )

        self.assertTrue(any("live sequence plan" in message for message in errors))
        self.assertTrue(any("Story index" in message for message in errors))
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story = root / "story.jsonl"
            plan = root / "live-sequence.json"
            story.touch()
            plan.touch()
            dialog.story_index.setText(str(story))
            dialog.live_sequence_plan.setText(str(plan))

            self.assertFalse(dialog.validation_errors())
            settings = dialog.settings()

        self.assertEqual(settings.live_sequence_mode, "shadow")
        self.assertEqual(settings.live_sequence_plan, str(plan))
        delete_dialog(dialog)

    def test_sequence_audio_manual_disables_auto_advance_controls(self):
        dialog = SettingsDialog(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                live_sequence_mode="audio-manual",
                auto_advance_enabled=True,
            )
        )

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-manual")
        self.assertFalse(dialog.auto_advance.isEnabled())
        self.assertFalse(dialog.auto_advance.isChecked())
        self.assertFalse(dialog.auto_advance_key.isEnabled())
        self.assertFalse(dialog.auto_advance_delay.isEnabled())
        self.assertIn("never sends advance keys", dialog.auto_advance.toolTip())
        self.assertFalse(dialog.auto_advance_reason.isHidden())
        self.assertIn("never sends advance keys", dialog.auto_advance_reason.text())
        self.assertFalse(dialog.settings().auto_advance_enabled)
        delete_dialog(dialog)

    def test_sequence_audio_auto_keeps_guarded_auto_advance_opt_in(self):
        dialog = SettingsDialog(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                live_sequence_mode="audio-auto",
                auto_advance_enabled=False,
            )
        )

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-auto")
        self.assertTrue(dialog.auto_advance.isEnabled())
        self.assertFalse(dialog.auto_advance.isChecked())
        self.assertFalse(dialog.auto_advance_key.isEnabled())
        self.assertIn("at most one key", dialog.auto_advance.toolTip())
        dialog.auto_advance.setChecked(True)
        self.assertTrue(dialog.auto_advance_key.isEnabled())
        self.assertTrue(dialog.auto_advance_delay.isEnabled())
        delete_dialog(dialog)

    def test_new_install_settings_recommend_guarded_sequence_auto(self):
        dialog = SettingsDialog(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
            )
        )

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-auto")
        self.assertIn("recommended", dialog.live_sequence_mode.currentText().casefold())
        self.assertTrue(dialog.auto_advance.isChecked())
        self.assertFalse(dialog.validation_errors())
        delete_dialog(dialog)

    def test_screen_capture_disables_auto_advance(self):
        dialog = SettingsDialog(
            AppSettings(capture_mode="screen", auto_advance_enabled=True)
        )

        self.assertFalse(dialog.auto_advance.isEnabled())
        self.assertFalse(dialog.auto_advance.isChecked())
        self.assertIn("selected game window", dialog.auto_advance.toolTip())
        self.assertFalse(dialog.auto_advance_reason.isHidden())
        self.assertIn("selected game window", dialog.auto_advance_reason.text())
        self.assertFalse(dialog.settings().auto_advance_enabled)
        delete_dialog(dialog)

    def test_settings_narrator_picker_stages_voice_and_preserves_other_edits(self):
        original = AppSettings(speech_backend="moss-tts")
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        dialog = SettingsDialog(
            original,
            voice_library=VoiceLibrary(Path(directory.name) / "voices"),
        )
        dialog.output_volume.setValue(37)
        dialog.section_navigation.setCurrentIndex(2)
        self.assertTrue(dialog.narrator_reference.isHidden())
        candidate = original.updated(
            speech_backend="pocket-tts",
            tts_model=None,
            tts_profile="default",
            voice_manifest="chosen-voices.json",
        )
        with (
            patch("vntts.app.GameNarratorDialog") as picker,
            patch.object(AppSettings, "save") as save,
        ):
            picker.return_value.exec.return_value = SettingsDialog.DialogCode.Accepted
            picker.return_value.result_settings = candidate
            dialog.choose_narrator_button.click()
            passed_settings, parent = picker.call_args.args
            self.assertEqual(passed_settings.output_volume_percent, 37)
            self.assertIs(parent, dialog)
            save.assert_not_called()
        draft = dialog._raw_settings()
        self.assertEqual(draft.voice_manifest, "chosen-voices.json")
        self.assertEqual(draft.speech_backend, "pocket-tts")
        self.assertIsNone(draft.tts_model)
        self.assertEqual(draft.tts_profile, "default")
        self.assertEqual(draft.output_volume_percent, 37)
        self.assertIsNone(draft.tts_speaker_wav)
        self.assertIn("Alba", dialog.narrator_voice.text())
        self.assertFalse(
            any(
                widget is dialog.choose_narrator_button
                for _, widget, _ in dialog.validation_errors()
            )
        )
        candidate = candidate.updated(
            speech_backend="moss-tts", tts_model="custom-moss", tts_profile="natural"
        )
        with patch("vntts.app.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = SettingsDialog.DialogCode.Accepted
            picker.return_value.result_settings = candidate
            dialog.choose_narrator_button.click()
        draft = dialog._raw_settings()
        self.assertEqual(draft.speech_backend, "moss-tts")
        self.assertEqual(draft.tts_model, "custom-moss")
        self.assertEqual(draft.tts_profile, "natural")
        self.assertEqual(draft.output_volume_percent, 37)
        before = dialog._raw_settings()
        with patch("vntts.app.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = SettingsDialog.DialogCode.Rejected
            dialog.choose_narrator_button.click()
        self.assertEqual(dialog._raw_settings(), before)
        dialog.reject()
        delete_dialog(dialog)

    def test_character_choice_keeps_manual_narrator_reference_in_settings(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "narrator.wav"
            reference.touch()
            original = AppSettings(tts_speaker_wav=str(reference))
            candidate = original
            dialog = SettingsDialog(original)
            dialog.advanced_narrator.setChecked(True)
            with patch("vntts.app.GameNarratorDialog") as picker:
                picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
                picker.return_value.result_settings = candidate
                dialog.choose_narrator_button.click()
            draft = dialog._raw_settings()
            self.assertEqual(draft.tts_speaker_wav, str(reference))
            self.assertTrue(dialog.advanced_narrator.isChecked())
            delete_dialog(dialog)

    def test_qwen_accepts_manual_narrator_without_transcript(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "narrator.wav"
            reference.touch()
            for live_backend, offline_backend in (
                ("qwen-tts", "pocket-tts"),
                ("pocket-tts", "qwen-tts"),
            ):
                dialog = SettingsDialog(
                    AppSettings(
                        speech_backend=live_backend,
                        offline_speech_backend=offline_backend,
                        tts_speaker_wav=str(reference),
                    ),
                    voice_library=VoiceLibrary(Path(directory) / "voices"),
                )
                self.assertFalse(
                    any(
                        widget is dialog.choose_narrator_button
                        for _, widget, _ in dialog.validation_errors()
                    )
                )
                delete_dialog(dialog)

    def test_offline_qwen_voice_picker_keeps_live_engine(self):
        original = AppSettings(
            speech_backend="pocket-tts", offline_speech_backend="qwen-tts"
        )
        dialog = SettingsDialog(original)
        with patch("vntts.app.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            picker.return_value.result_settings = original.updated(
                speech_backend="qwen-tts"
            )
            dialog.choose_narrator()
            self.assertTrue(picker.call_args.kwargs["use_offline_engine"])
        self.assertEqual(dialog._raw_settings().speech_backend, "pocket-tts")
        self.assertEqual(dialog._raw_settings().offline_speech_backend, "qwen-tts")
        delete_dialog(dialog)

    def test_settings_missing_moss_voice_targets_picker_and_file_is_optional(self):
        empty_directory = TemporaryDirectory()
        self.addCleanup(empty_directory.cleanup)
        dialog = SettingsDialog(
            AppSettings(speech_backend="moss-tts"),
            voice_library=VoiceLibrary(Path(empty_directory.name) / "voices"),
        )
        self.assertTrue(
            any(
                widget is dialog.choose_narrator_button
                for _, widget, _ in dialog.validation_errors()
            )
        )
        dialog.advanced_narrator.setChecked(True)
        self.assertFalse(dialog.narrator_reference.isHidden())
        dialog.advanced_settings.setChecked(True)
        dialog.advanced_settings.setChecked(False)
        self.assertTrue(dialog.narrator_reference.isHidden())
        dialog.advanced_settings.setChecked(True)
        self.assertFalse(dialog.narrator_reference.isHidden())
        with patch("vntts.app.QFileDialog.getOpenFileName", return_value=("", "")):
            dialog.browse_narrator_reference()
        dialog.narrator_reference.setText("custom.wav")
        dialog.narrator_reference.textEdited.emit("custom.wav")
        self.assertTrue(
            any(
                widget is dialog.narrator_reference
                for _, widget, _ in dialog.validation_errors()
            )
        )
        delete_dialog(dialog)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        library = VoiceLibrary(Path(directory.name) / "voices")
        library.select("Narrator", route="voice", source_id="character:rhiannon")
        dialog = SettingsDialog(
            AppSettings(speech_backend="moss-tts", tts_speaker_wav="custom.wav"),
            voice_library=library,
        )
        self.assertFalse(
            any(
                widget is dialog.narrator_reference
                for _, widget, _ in dialog.validation_errors()
            )
        )
        delete_dialog(dialog)

    def test_settings_offer_moss_with_model_language_and_reference(self):
        dialog = SettingsDialog(
            AppSettings(
                speech_backend="moss-tts",
                tts_speaker_wav="matilda.wav",
            )
        )

        self.assertEqual(dialog.speech_backend.currentData(), "moss-tts")
        self.assertTrue(dialog.tts_model.isEnabled())
        self.assertTrue(dialog.tts_language.isEnabled())
        self.assertTrue(dialog.narrator_reference.isEnabled())
        self.assertTrue(dialog.tts_profile.isEnabled())
        self.assertIn("MOSS-TTS-Local-Transformer", dialog.tts_model.text())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertEqual(dialog.settings().tts_speaker_wav, "matilda.wav")
        delete_dialog(dialog)

    def test_settings_switch_default_model_between_xtts_and_moss(self):
        dialog = SettingsDialog(
            AppSettings(
                speech_backend="coqui-xtts",
                tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
            )
        )

        dialog.speech_backend.setCurrentIndex(
            dialog.speech_backend.findData("moss-tts")
        )
        self.assertIn("MOSS-TTS-Local-Transformer", dialog.tts_model.text())
        dialog.speech_backend.setCurrentIndex(
            dialog.speech_backend.findData("coqui-xtts")
        )
        self.assertEqual(
            dialog.tts_model.text(),
            "tts_models/multilingual/multi-dataset/xtts_v2",
        )
        delete_dialog(dialog)

    def test_settings_control_macos_launch_at_login(self):
        dialog = SettingsDialog(AppSettings(launch_at_login=True))

        self.assertTrue(dialog.launch_at_login.isChecked())
        dialog.launch_at_login.setChecked(False)

        self.assertFalse(dialog.settings().launch_at_login)
        delete_dialog(dialog)

    def test_macos_settings_explain_control_window_only_hotkeys(self):
        with patch("vntts.app.sys.platform", "darwin"):
            dialog = SettingsDialog(AppSettings())

        self.assertFalse(dialog.macos_hotkey_notice.isHidden())
        self.assertIn(
            "Global hotkeys are unavailable", dialog.macos_hotkey_notice.text()
        )
        self.assertIn("macOS controls", dialog.macos_hotkey_notice.text())
        self.assertIn("compact controls", dialog.macos_hotkey_notice.text())
        self.assertIs(
            dialog.settings_regions[0].layout().itemAt(0).widget(),
            dialog.macos_hotkey_notice,
        )
        self.assertTrue(
            all(not recorder.isEnabled() for recorder in dialog.hotkey_recorders)
        )
        delete_dialog(dialog)

    def test_composite_settings_fields_have_accessible_labels(self):
        dialog = SettingsDialog(AppSettings())
        expected = {
            "Screenshot directory": dialog.screenshot_directory,
            "Game window": dialog.game_window,
            "Diagnostics directory": dialog.ocr_diagnostics_directory,
            "Narrator reference": dialog.narrator_reference,
            "Game pack": dialog.game_pack,
            "Voice manifest": dialog.voice_manifest,
            "Story index": dialog.story_index,
            "Live speaker corpus": dialog.live_speaker_corpus,
            "Generated audio manifest": dialog.generated_audio_manifest,
        }
        labels = {
            label.text(): label
            for label in dialog.findChildren(QLabel)
            if label.text() in expected
        }

        for name, field in expected.items():
            self.assertIs(labels[name].buddy(), field)
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
        for button in (
            dialog.screenshot_browse_button,
            dialog.refresh_windows_button,
            dialog.diagnostics_browse_button,
            dialog.narrator_reference_button,
            dialog.game_pack_button,
            dialog.voice_manifest_button,
            dialog.story_index_button,
            dialog.live_speaker_corpus_button,
            dialog.generated_audio_manifest_button,
        ):
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.accessibleDescription())
        delete_dialog(dialog)

    def test_settings_change_updates_macos_launch_at_login(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(launch_at_login=False),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        updated = AppSettings(launch_at_login=True)
        dialog.settings.return_value = updated

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch("vntts.app.configure_macos_launch_at_login") as configure,
            patch.object(tray_application, "start_hotkeys"),
            patch(
                "vntts.app.TrayApplication._save_settings_candidate",
                return_value=Path("settings.json"),
            ),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        configure.assert_called_once_with(True)
        controller.apply_settings.assert_called_once_with(updated, cancellation=ANY)
        self.assertEqual(tray_application.settings, updated)
        tray_application.shutdown()

    def test_failed_settings_write_rolls_back_launch_at_login_before_publish(self):
        original = AppSettings(launch_at_login=False)
        controller = Mock(settings=original)
        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.settings.return_value = original.updated(launch_at_login=True)

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch("vntts.app.configure_macos_launch_at_login") as configure,
            patch(
                "vntts.app.TrayApplication._save_settings_candidate",
                side_effect=OSError("disk full"),
            ),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        self.assertEqual(configure.call_args_list, [call(True), call(False)])
        self.assertIs(tray_application.settings, original)
        controller.apply_settings.assert_not_called()
        self.assertIn("disk full", tray_application.status_action.text())
        tray_application.shutdown()

    def test_backend_setting_reloads_speech_without_app_restart(self):
        controller = Mock()
        controller.settings = AppSettings(speech_backend="pocket-tts")
        controller.is_ready = True
        controller.start.return_value = True
        tray_application = TrayApplication(
            self.application,
            controller.settings,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        updated = controller.settings.updated(speech_backend="moss-tts")
        dialog.settings.return_value = updated
        tray_application._controller_ready = True

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch.object(tray_application, "start_hotkeys"),
            patch(
                "vntts.app.TrayApplication._save_settings_candidate",
                return_value=Path("settings.json"),
            ),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        controller.apply_settings.assert_called_once_with(updated, cancellation=ANY)
        controller.shutdown.assert_called_once_with()
        controller.start.assert_called_once_with()
        self.assertEqual(tray_application.settings.speech_backend, "moss-tts")
        self.assertIn("speech reloaded", tray_application.status_action.toolTip())
        tray_application.shutdown()

    def test_saved_backend_retry_reloads_restored_runtime(self):
        previous = AppSettings(speech_backend="pocket-tts")
        requested = previous.updated(speech_backend="moss-tts")
        for first_outcome in (False, RuntimeError("startup failed"), "cancel"):
            with self.subTest(first_outcome=first_outcome):
                controller = Mock(settings=previous, is_ready=True)
                controller.is_live_running = False

                def apply(settings, **_options):
                    controller.settings = settings
                    return True

                controller.apply_settings.side_effect = apply
                controller.start.side_effect = (
                    [RuntimeError("startup failed"), True, True]
                    if isinstance(first_outcome, Exception)
                    else [False, True, True]
                )
                tray = TrayApplication(
                    self.application,
                    previous,
                    controller_factory=Mock(return_value=controller),
                )
                tray.set_ready(True)
                dialog = Mock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.settings.return_value = requested
                with (
                    patch("vntts.app.SettingsDialog", return_value=dialog),
                    patch.object(
                        tray,
                        "_save_settings_candidate",
                        return_value=Path("settings.json"),
                    ),
                ):
                    if first_outcome == "cancel":

                        def cancel_apply(settings, *, cancellation=None):
                            if cancellation is not None:
                                cancellation.set()
                                return False
                            return apply(settings)

                        controller.apply_settings.side_effect = cancel_apply
                        controller.start.side_effect = [True, True]
                    tray.open_settings()
                    self.wait_until(lambda: not tray.configuration_runner.active)
                    self.assertEqual(tray.settings, requested)
                    self.assertEqual(controller.settings, previous)

                    controller.apply_settings.side_effect = apply
                    controller.reset_mock()
                    dialog.settings.return_value = requested.updated(
                        output_volume_percent=42
                    )
                    tray.open_settings()
                    self.wait_until(lambda: not tray.configuration_runner.active)

                self.assertEqual(controller.settings, dialog.settings.return_value)
                controller.shutdown.assert_called_once_with()
                controller.start.assert_called_once_with()
                self.assertIn("speech reloaded", tray.status_action.toolTip())
                tray.shutdown()

    def test_macos_permission_action_opens_recovery_dialog(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        dialog = Mock()

        with patch("vntts.app.MacOSPermissionsDialog", return_value=dialog):
            tray_application.open_macos_permissions()

        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_narrator_picker_does_not_require_reading_engine(self):
        controller = Mock()
        controller.is_live_running = False
        controller.available_voice_characters.return_value = ["Narrator", "Marcus"]
        choices = [Mock(id="preset:alba", label="Alba")]
        controller.available_voice_choices.return_value = choices
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog) as factory,
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
        ):
            tray_application.open_voice_previews()
            self.assertIs(tray_application.narrator_dialog, dialog)
            tray_application._narrator_finished(QDialog.DialogCode.Rejected)

        factory.assert_called_once_with(
            tray_application.settings,
            tray_application.dashboard,
            use_offline_engine=False,
        )
        controller.start.assert_not_called()
        controller.available_voice_choices.assert_not_called()
        dialog.exec.assert_not_called()
        tray_application.shutdown()

    def test_narrator_voice_dialog_pauses_live_and_restores_it(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()
        controller.available_voice_characters.return_value = ["Narrator"]
        controller.available_voice_choices.return_value = []

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with (
            patch("vntts.app.GameNarratorDialog", return_value=dialog),
            patch.object(tray_application.dashboard, "embed_narrator"),
            patch.object(tray_application.dashboard, "remove_narrator"),
        ):
            tray_application.open_voice_previews()
            self.wait_until(lambda: tray_application.narrator_dialog is dialog)
            self.assertFalse(controller.is_live_running)
            tray_application._narrator_finished(QDialog.DialogCode.Rejected)
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with(timeout_seconds=5.0)
        tray_application.shutdown()

    def test_history_dialog_uses_controller_session_and_replay(self):
        controller = Mock()
        controller.is_live_running = False
        controller.is_one_shot_read_running = False
        controller.inspect_current_dialog.return_value = DiagnosticSnapshot(
            None,
            text="Fresh manual capture",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog) as factory:
            tray_application.open_history()

        factory.assert_called_once_with(
            controller.history,
            controller.replay_dialog,
            stop_handler=controller.stop_voice_preview,
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_history_waits_for_one_shot_read_before_opening(self):
        controller = Mock()
        controller.is_live_running = False
        controller.is_one_shot_read_running = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog) as factory:
            tray_application.open_history()
            self.assertIn("current dialog read", tray_application.status_action.text())
            controller.is_one_shot_read_running = False
            tray_application.open_history()

        factory.assert_called_once_with(
            controller.history,
            controller.replay_dialog,
            stop_handler=controller.stop_voice_preview,
        )
        dialog.exec.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_history_dialog_pauses_live_capture_and_restores_it_after_close(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.side_effect = lambda: self.assertFalse(controller.is_live_running)

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog):
            tray_application.open_history()
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with(timeout_seconds=5.0)
        tray_application.shutdown()

    def test_modal_dialog_blocks_background_read_starts_until_dismissed(self):
        controller = Mock(is_ready=True, is_live_running=False)
        controller.is_one_shot_read_running = False
        controller.unresolved_live_speakers.return_value = ()
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.set_ready(True)
        modal = QDialog()
        modal.setModal(True)
        live_started = True

        def try_background_actions():
            nonlocal live_started
            try:
                tray_application.read_once()
                live_started = tray_application.toggle_live()
                controller.is_live_running = True
                tray_application.toggle_live()
            finally:
                controller.is_live_running = False
                modal.accept()

        QTimer.singleShot(0, try_background_actions)
        with patch.object(tray_application, "_request_stop_reading") as stop_reading:
            modal.exec()

        stop_reading.assert_called_once_with()
        self.assertFalse(live_started)
        controller.read_once.assert_not_called()
        controller.toggle_live.assert_not_called()
        self.assertTrue(tray_application._runtime_control_state().ready)
        self.assertTrue(tray_application.read_action.isEnabled())
        self.assertTrue(tray_application.live_action.isEnabled())
        tray_application.read_once()
        self.assertTrue(tray_application.toggle_live())
        controller.read_once.assert_called_once_with()
        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_history_dialog_restores_live_capture_after_construction_failure(self):
        controller = Mock()
        controller.is_live_running = False
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        self.addCleanup(tray_application.shutdown)

        with (
            patch(
                "vntts.app.DialogueHistoryDialog", side_effect=RuntimeError("failed")
            ),
            patch.object(tray_application, "toggle_live") as resume,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                tray_application._open_history_dialog(True)

        resume.assert_called_once_with()

    def test_support_bundle_export_runs_with_sanitized_runtime_inputs(self):
        controller = Mock()
        diagnostic = DiagnosticSnapshot(None, confidence=88)
        controller.get_latest_diagnostic.return_value = diagnostic
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        builder = Mock()
        builder.build.return_value = Path("support.zip")
        tray_application.support_dialog = Mock()

        with (
            patch(
                "vntts.app.QFileDialog.getSaveFileName",
                return_value=("support.zip", "ZIP archives (*.zip)"),
            ),
            patch("vntts.app.SupportBundleBuilder", return_value=builder) as factory,
        ):
            tray_application.export_support_bundle()
            self.wait_until(lambda: not tray_application.support_export_runner.active)

        factory.assert_called_once_with(
            tray_application.settings,
            tray_application.support_log,
            diagnostic=diagnostic,
            generation_timelines=tray_application.generation_timelines,
            previous_session=tray_application.previous_session,
            audio_lifecycle=tray_application.audio_lifecycle,
            voice_library=tray_application.controller.voice_library,
        )
        builder.build.assert_called_once_with("support.zip")
        tray_application.support_dialog.set_export_result.assert_called_once_with(
            True,
            "support.zip",
        )
        self.assertIn("Support bundle saved", tray_application.status_action.text())
        tray_application.shutdown()

    def test_cancelled_support_export_restores_dialog_action(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()

        with patch(
            "vntts.app.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ):
            tray_application.export_support_bundle()

        tray_application.support_dialog.set_export_result.assert_called_once_with(
            None,
            "Support report export cancelled.",
        )
        tray_application.shutdown()

    def test_support_export_reports_worker_failure(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()
        builder = Mock()
        builder.build.side_effect = OSError("disk full")

        with (
            patch(
                "vntts.app.QFileDialog.getSaveFileName",
                return_value=("support.zip", "ZIP archives (*.zip)"),
            ),
            patch("vntts.app.SupportBundleBuilder", return_value=builder),
        ):
            tray_application.export_support_bundle()
            self.wait_until(lambda: not tray_application.support_export_runner.active)

        tray_application.support_dialog.set_export_result.assert_called_once_with(
            False, "disk full"
        )
        tray_application.shutdown()

    def test_support_export_finishes_in_background_after_shutdown(self):

        pool = ManualThreadPool()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()
        tray_application.support_export_runner = LatestTaskRunner(
            tray_application,
            thread_pool=pool,
        )
        tray_application.support_export_runner.finished.connect(
            tray_application._support_export_finished
        )
        builder = Mock()
        builder.build.return_value = Path("support.zip")

        with (
            patch(
                "vntts.app.QFileDialog.getSaveFileName",
                return_value=("support.zip", "ZIP archives (*.zip)"),
            ),
            patch("vntts.app.SupportBundleBuilder", return_value=builder),
        ):
            tray_application.export_support_bundle()
            tray_application.shutdown()
            pool.tasks.pop(0).run()
            self.application.processEvents()

        builder.build.assert_called_once_with("support.zip")
        tray_application.support_dialog.set_export_result.assert_not_called()

    def test_support_launch_results_return_to_the_support_dialog(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()

        with patch.object(
            tray_application,
            "open_diagnostics",
            side_effect=OSError("capture unavailable"),
        ):
            tray_application.open_support_diagnostics()
        tray_application.support_dialog.set_launcher_result.assert_called_with(
            "diagnostics",
            False,
            "Unable to open live diagnostics: capture unavailable",
        )

        tray_application.support_dialog.reset_mock()
        settings_path = Path("/tmp/vntts-settings")
        with patch.object(
            tray_application,
            "open_settings_folder",
            return_value=settings_path,
        ):
            tray_application.open_support_settings_folder()
        tray_application.support_dialog.set_launcher_result.assert_called_once_with(
            "settings-folder",
            True,
            f"Settings folder opened: {settings_path}",
        )
        tray_application.shutdown()

    def test_settings_folder_uses_path_selected_at_startup(self):
        with TemporaryDirectory() as directory:
            settings_path = Path(directory) / "initial" / "settings.json"
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(settings_path)}):
                tray = TrayApplication(
                    self.application,
                    AppSettings(),
                    controller_factory=Mock(return_value=Mock()),
                )
            with (
                patch.dict(
                    os.environ,
                    {"VNTTS_SETTINGS_FILE": str(Path(directory) / "other.json")},
                ),
                patch(
                    "vntts.app.QDesktopServices.openUrl", return_value=True
                ) as open_url,
            ):
                self.assertEqual(tray.open_settings_folder(), settings_path.parent)
            self.assertEqual(
                Path(open_url.call_args.args[0].toLocalFile()), settings_path.parent
            )
            tray.shutdown()

    def test_settings_shortcut_errors_identify_each_empty_recorder(self):
        dialog = SettingsDialog(AppSettings())
        dialog.live_hotkey.clear()

        errors = dialog.validation_errors()
        self.assertEqual(
            [(widget, message) for _, widget, message in errors],
            [
                (
                    dialog.live_hotkey,
                    "Keyboard shortcuts: Live reading: press a shortcut.",
                )
            ],
        )
        dialog.read_hotkey.clear()
        self.assertEqual(
            [widget for _, widget, _ in dialog.validation_errors()],
            [dialog.read_hotkey, dialog.live_hotkey],
        )
        delete_dialog(dialog)

    def test_incomplete_shortcuts_do_not_block_settings_previews(self):
        original = AppSettings()
        dialog = SettingsDialog(original)
        dialog.live_hotkey.clear()
        dialog.output_volume.setValue(37)
        errors = dialog.update_validation_summary()
        self.assertEqual([widget for _, widget, _ in errors], [dialog.live_hotkey])
        self.assertIn("Live reading", dialog.validation_summary.text())
        with patch("vntts.app.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Rejected
            dialog.choose_narrator()
            draft = picker.call_args.args[0]
            self.assertEqual(draft.output_volume_percent, 37)
            self.assertEqual(draft.live_hotkey, original.live_hotkey)
        with self.assertRaisesRegex(ValueError, "press a shortcut"):
            dialog.settings()
        with TemporaryDirectory() as directory:
            dialog.game_pack.setText(str(Path(directory) / "missing-pack.json"))
            errors = dialog.update_validation_summary()
            self.assertIn(dialog.game_pack, [widget for _, widget, _ in errors])
        dialog.game_pack.clear()
        dialog.read_hotkey.set_hotkey("<ctrl>+j")
        dialog.live_hotkey.set_hotkey("<ctrl>+k")
        self.assertEqual(dialog.settings().read_hotkey, "<ctrl>+j")
        self.assertEqual(dialog.settings().live_hotkey, "<ctrl>+k")
        delete_dialog(dialog)

    def test_settings_reject_duplicate_recorded_hotkeys(self):
        dialog = SettingsDialog(AppSettings())
        dialog.live_hotkey.set_hotkey(dialog.read_hotkey.hotkey())

        dialog.validate_and_accept()

        self.assertIn("duplicates", dialog.validation_summary.text())
        self.assertEqual(dialog.section_navigation.currentIndex(), 0)
        self.assertNotEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
        delete_dialog(dialog)

    def test_recognized_dialog_has_a_dedicated_tray_status(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )

        tray_application.signals.dialog_changed.emit("Marcus", "Ready to continue.")
        tray_application.signals.status_changed.emit("Speech queue cleared")

        self.assertEqual(
            tray_application.dialog_action.text(),
            "Marcus: Ready to continue.",
        )
        self.assertEqual(tray_application.status_action.text(), "Speech queue cleared")
        tray_application.shutdown()

    def test_tray_can_toggle_auto_advance_without_restarting_live_mode(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                auto_advance_enabled=False,
            ),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.TrayApplication._save_settings_candidate") as save:
            tray_application.auto_advance_action.setChecked(True)

        controller.set_auto_advance_enabled.assert_called_once_with(True)
        self.assertTrue(tray_application.settings.auto_advance_enabled)
        self.assertTrue(tray_application.dashboard.auto_advance_check.isChecked())
        save.assert_called_once_with(tray_application.settings)
        tray_application.shutdown()

    def test_dashboard_can_toggle_auto_advance_while_reading(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                auto_advance_enabled=False,
            ),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.TrayApplication._save_settings_candidate") as save:
            tray_application.dashboard.auto_advance_check.setChecked(True)

        self.assertTrue(tray_application.auto_advance_action.isChecked())
        self.assertTrue(tray_application.settings.auto_advance_enabled)
        controller.set_auto_advance_enabled.assert_called_once_with(True)
        save.assert_called_once_with(tray_application.settings)
        tray_application.shutdown()

    def test_auto_advance_waits_for_running_configuration_apply(self):
        controller = Mock()
        controller.settings = AppSettings(capture_mode="screen")
        tray_application = TrayApplication(
            self.application,
            AppSettings(capture_mode="window", auto_advance_enabled=False),
            controller_factory=Mock(return_value=controller),
        )
        tray_application._controller_busy = True
        tray_application._update_auto_advance_action()

        self.assertFalse(tray_application.dashboard.auto_advance_check.isEnabled())
        self.assertFalse(tray_application.auto_advance_action.isEnabled())
        with patch("vntts.app.TrayApplication._save_settings_candidate") as save:
            tray_application.toggle_auto_advance(True)

        save.assert_not_called()
        controller.set_auto_advance_enabled.assert_not_called()
        self.assertFalse(tray_application.settings.auto_advance_enabled)
        self.assertFalse(tray_application.dashboard.auto_advance_check.isChecked())
        self.assertIn("Try again when ready", tray_application.status_action.text())

        tray_application._controller_busy = False
        tray_application._update_auto_advance_action()
        self.assertTrue(tray_application.dashboard.auto_advance_check.isEnabled())
        tray_application.shutdown()

    def test_failed_dashboard_auto_advance_write_restores_checkbox(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(capture_mode="window", auto_advance_enabled=False),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.TrayApplication._save_settings_candidate",
            side_effect=OSError("read-only directory"),
        ):
            tray_application.dashboard.auto_advance_check.setChecked(True)

        self.assertFalse(tray_application.dashboard.auto_advance_check.isChecked())
        self.assertFalse(tray_application.auto_advance_action.isChecked())
        self.assertFalse(tray_application.settings.auto_advance_enabled)
        controller.set_auto_advance_enabled.assert_not_called()
        tray_application.shutdown()

    def test_failed_auto_advance_write_restores_action_without_runtime_change(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                auto_advance_enabled=False,
            ),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.TrayApplication._save_settings_candidate",
            side_effect=OSError("read-only directory"),
        ):
            tray_application.auto_advance_action.setChecked(True)

        self.assertFalse(tray_application.auto_advance_action.isChecked())
        self.assertFalse(tray_application.dashboard.auto_advance_check.isChecked())
        self.assertFalse(tray_application.settings.auto_advance_enabled)
        controller.set_auto_advance_enabled.assert_not_called()
        self.assertIn("read-only directory", tray_application.status_action.text())
        tray_application.shutdown()

    def test_tray_rejects_auto_advance_without_capture_authority(self):
        cases = (
            ("screen", "audio-auto", "selected game window"),
            ("window", "audio-manual", "never sends advance keys"),
        )
        for capture_mode, sequence_mode, expected_reason in cases:
            with self.subTest(capture_mode=capture_mode, sequence_mode=sequence_mode):
                controller = Mock()
                controller_factory = Mock(return_value=controller)
                tray_application = TrayApplication(
                    self.application,
                    AppSettings(
                        capture_mode=capture_mode,
                        game_window_title="Reverse: 1999",
                        live_sequence_mode=sequence_mode,
                        auto_advance_enabled=True,
                    ),
                    controller_factory=controller_factory,
                )

                self.assertFalse(tray_application.auto_advance_action.isEnabled())
                self.assertFalse(tray_application.auto_advance_action.isChecked())
                self.assertFalse(
                    tray_application.dashboard.auto_advance_check.isEnabled()
                )
                self.assertFalse(
                    tray_application.dashboard.auto_advance_check.isChecked()
                )
                self.assertTrue(tray_application.auto_advance_reason_action.isVisible())
                self.assertIn(
                    expected_reason,
                    tray_application.auto_advance_reason_action.text(),
                )
                with patch(
                    "vntts.app.TrayApplication._save_settings_candidate"
                ) as save:
                    tray_application.toggle_auto_advance(True)
                save.assert_not_called()
                controller.set_auto_advance_enabled.assert_not_called()
                self.assertFalse(tray_application.auto_advance_action.isChecked())
                tray_application.shutdown()

    def test_failed_profile_asset_and_compact_writes_do_not_publish(self):
        original = AppSettings(compact_controls=False)
        candidate = original.updated(game_window_title="Changed")
        dialog_cases = (
            ("GameProfilesDialog", "open_profiles"),
            ("AssetManagerDialog", "open_assets"),
        )
        for dialog_name, method_name in dialog_cases:
            with self.subTest(method=method_name):
                controller = Mock(settings=original)
                tray_application = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                dialog = Mock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.settings.return_value = candidate
                with (
                    patch(f"vntts.app.{dialog_name}", return_value=dialog),
                    patch.object(
                        tray_application.profile_store,
                        "get",
                        return_value=Mock(
                            dialog_region=DialogRegion(0.1, 0.6, 0.8, 0.3)
                        ),
                    ),
                    patch(
                        "vntts.app.TrayApplication._save_settings_candidate",
                        side_effect=OSError("disk full"),
                    ),
                ):
                    getattr(tray_application, method_name)()

                self.assertIs(tray_application.settings, original)
                controller.apply_settings.assert_not_called()
                self.assertFalse(tray_application.profile_restart_runner.active)
                self.assertIn("disk full", tray_application.status_action.text())
                tray_application.shutdown()

        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=Mock(settings=original)),
        )
        with patch(
            "vntts.app.TrayApplication._save_settings_candidate",
            side_effect=OSError("disk full"),
        ):
            tray_application._save_compact_preference(True)
        self.assertIs(tray_application.settings, original)
        self.assertIn("disk full", tray_application.status_action.text())
        tray_application.shutdown()

    def test_live_diagnostics_refresh_captures_a_fresh_snapshot(self):
        stale = DiagnosticSnapshot(None, text="Already captured")
        fresh = DiagnosticSnapshot(None, text="Fresh capture")
        controller = Mock()
        controller.is_live_running = True
        controller.get_latest_diagnostic.return_value = stale
        controller.inspect_current_dialog.return_value = fresh
        controller.commit_diagnostic_snapshot.return_value = fresh
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        diagnostics_dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = diagnostics_dialog

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: args[-1](),
            ),
        ):
            tray_application.refresh_diagnostics()
            self.wait_until(
                lambda: not tray_application.diagnostics_refresh_runner.active
            )

        controller.inspect_current_dialog.assert_called_once_with(
            notify=False, publish=False
        )
        controller.commit_diagnostic_snapshot.assert_called_once_with(fresh)
        diagnostics_dialog.set_snapshot.assert_called_once_with(fresh)
        diagnostics_dialog.conceal_for_capture.assert_called_once_with()
        tray_application.shutdown()

    def test_manual_diagnostics_hides_window_before_capture(self):
        controller = Mock()
        controller.is_live_running = False
        controller.inspect_current_dialog.return_value = DiagnosticSnapshot(
            None,
            text="Fresh manual capture",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        diagnostics_dialog = Mock()
        tray_application.diagnostics_dialog = diagnostics_dialog

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: args[-1](),
            ),
        ):
            tray_application.refresh_diagnostics()
            self.wait_until(
                lambda: not tray_application.diagnostics_refresh_runner.active
            )

        diagnostics_dialog.conceal_for_capture.assert_called_once_with()
        controller.inspect_current_dialog.assert_called_once_with(
            notify=False, publish=False
        )
        tray_application.shutdown()

    def test_diagnostic_refresh_keeps_latest_result_and_drops_after_close(self):

        pool = ManualThreadPool()
        controller = Mock()
        controller.commit_diagnostic_snapshot.side_effect = lambda snapshot: snapshot
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        diagnostics_dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = diagnostics_dialog
        tray_application.diagnostics_refresh_runner = LatestTaskRunner(
            tray_application,
            thread_pool=pool,
        )
        tray_application.diagnostics_refresh_runner.finished.connect(
            tray_application._diagnostics_refresh_finished
        )
        tray_application.diagnostics_refresh_generation = 1
        tray_application.diagnostics_refresh_runner.start(
            lambda: DiagnosticSnapshot(None, text="Stale capture")
        )
        tray_application.diagnostics_refresh_generation = 2
        tray_application.diagnostics_refresh_runner.start(
            lambda: DiagnosticSnapshot(None, text="Latest capture")
        )

        pool.tasks.pop(0).run()
        self.application.processEvents()
        diagnostics_dialog.set_snapshot.assert_not_called()

        pool.tasks.pop(0).run()
        self.application.processEvents()
        diagnostics_dialog.set_snapshot.assert_called_once()

        diagnostics_dialog.reset_mock()
        tray_application.diagnostics_refresh_generation = 3
        tray_application.diagnostics_refresh_runner.start(
            lambda: DiagnosticSnapshot(None, text="Closed capture")
        )
        tray_application._diagnostics_closed(diagnostics_dialog)
        pool.tasks.pop(0).run()
        self.application.processEvents()

        diagnostics_dialog.set_snapshot.assert_not_called()
        tray_application.shutdown()

    def test_closed_diagnostics_dialog_drops_its_pending_result(self):

        pool = ManualThreadPool()
        controller = AppController(AppSettings())
        controller.last_diagnostic = DiagnosticSnapshot(None, text="Current capture")
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.diagnostics_refresh_runner = LatestTaskRunner(
            tray_application,
            thread_pool=pool,
        )
        tray_application.diagnostics_refresh_runner.finished.connect(
            tray_application._diagnostics_refresh_finished
        )
        tray_application.open_diagnostics()
        dialog = tray_application.diagnostics_dialog

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: args[-1](),
            ),
            patch.object(dialog, "set_snapshot") as set_snapshot,
        ):
            dialog.request_refresh()
            dialog.close()
            with patch(
                "vntts.controller_components.analyze_dialog_snapshot",
                side_effect=lambda *_args, diagnostic_handler, **_kwargs: (
                    diagnostic_handler(DiagnosticSnapshot(None, text="Closed capture"))
                ),
            ):
                pool.tasks.pop(0).run()
            self.application.processEvents()

        self.assertFalse(tray_application.diagnostics_refresh_runner.active)
        self.assertEqual(controller.get_latest_diagnostic().text, "Current capture")
        set_snapshot.assert_not_called()
        tray_application.shutdown()

    def test_diagnostics_retry_invalidates_worker_before_deferred_launch(self):

        pool = ManualThreadPool()
        controller = Mock()
        controller.get_latest_diagnostic.return_value = None
        controller.inspect_current_dialog.side_effect = (
            DiagnosticSnapshot(None, text="Obsolete capture"),
            DiagnosticSnapshot(None, text="Retry capture"),
        )
        controller.commit_diagnostic_snapshot.side_effect = lambda snapshot: snapshot
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray.diagnostics_refresh_runner = LatestTaskRunner(tray, thread_pool=pool)
        tray.diagnostics_refresh_runner.finished.connect(
            tray._diagnostics_refresh_finished
        )
        tray.open_diagnostics()
        dialog = tray.diagnostics_dialog
        pending = []
        try:
            with (
                patch("vntts.app.macos_permission_warnings", return_value=[]),
                patch(
                    "vntts.app.get_macos_permission_status",
                    return_value={"screen_capture": True},
                ),
                patch(
                    "vntts.app.QTimer.singleShot",
                    side_effect=lambda _delay, *args: pending.append(args[-1]),
                ),
            ):
                dialog.request_refresh()
                pending.pop()()
                dialog._refresh_timed_out(dialog.refresh_generation)
                dialog.request_refresh()
                pool.tasks.pop().run()
                self.application.processEvents()
                controller.commit_diagnostic_snapshot.assert_not_called()
                self.assertTrue(dialog.refresh_in_flight)

                pending.pop()()
                pool.tasks.pop().run()
                self.application.processEvents()
                self.assertEqual(dialog.text.text(), "Retry capture")
                self.assertFalse(dialog.refresh_in_flight)

                controller.inspect_current_dialog.reset_mock()
                dialog.request_refresh()
                dialog._refresh_timed_out(dialog.refresh_generation)
                pending.pop()()
                self.assertEqual(pool.tasks, [])
                controller.inspect_current_dialog.assert_not_called()
        finally:
            tray.shutdown()
            dialog.deleteLater()

    def test_closed_diagnostics_dialog_cancels_deferred_capture(self) -> None:
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = dialog
        runner = Mock(active=False)
        tray_application.diagnostics_refresh_runner = runner
        callbacks = []

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: callbacks.append(args[-1]),
            ),
        ):
            tray_application.refresh_diagnostics()
            tray_application._diagnostics_closed(dialog)
            callbacks.pop()()

        runner.start.assert_not_called()
        tray_application.shutdown()

    def test_diagnostics_close_cancels_delayed_capture_before_worker_starts(self):
        controller = Mock()
        controller.get_latest_diagnostic.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.open_diagnostics()
        dialog = tray_application.diagnostics_dialog
        pending = []

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: pending.append(args[-1]),
            ),
        ):
            dialog.request_refresh()
            dialog.close()
            self.application.processEvents()
            pending.pop()()

        controller.inspect_current_dialog.assert_not_called()
        self.assertFalse(tray_application.diagnostics_refresh_runner.active)
        tray_application.shutdown()

    def test_empty_diagnostics_result_is_reported(self) -> None:
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = dialog

        tray_application._diagnostics_refresh_finished(None, None)

        dialog.set_warning.assert_called_once()
        self.assertIn("no result", dialog.set_warning.call_args.args[0])
        tray_application.shutdown()

    def test_live_diagnostics_update_does_not_complete_manual_refresh(self):
        controller = Mock()
        controller.commit_diagnostic_snapshot.side_effect = lambda snapshot: snapshot
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = dialog
        live = DiagnosticSnapshot(None, text="Older live update")
        manual = DiagnosticSnapshot(None, text="Fresh manual capture")

        tray_application.update_diagnostics_snapshot(live)
        dialog.set_snapshot.assert_not_called()
        tray_application._diagnostics_refresh_finished(manual, None)

        dialog.set_snapshot.assert_called_once_with(manual)
        tray_application.shutdown()

    def test_open_diagnostics_keeps_permission_warning_with_existing_snapshot(self):
        controller = Mock()
        controller.get_latest_diagnostic.return_value = DiagnosticSnapshot(
            None, text="Earlier captured dialogue"
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.macos_permission_warnings",
            return_value=["Screen Recording permission is missing"],
        ):
            tray_application.open_diagnostics()

        dialog = tray_application.diagnostics_dialog
        self.assertEqual(dialog.text.text(), "Earlier captured dialogue")
        self.assertIn("Screen Recording permission", dialog.warning.text())
        self.assertTrue(dialog.warning.isVisible())
        tray_application.shutdown()

    def test_reopening_diagnostics_does_not_discard_pending_capture(self):
        release = Event()
        controller = Mock()
        controller.commit_diagnostic_snapshot.side_effect = lambda snapshot: snapshot
        controller.get_latest_diagnostic.return_value = DiagnosticSnapshot(
            None, text="Previous capture"
        )

        def inspect(*, notify, publish):
            release.wait(10)
            return DiagnosticSnapshot(None, text="Fresh capture")

        controller.inspect_current_dialog.side_effect = inspect
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.open_diagnostics()
        dialog = tray_application.diagnostics_dialog

        try:
            with (
                patch(
                    "vntts.app.get_macos_permission_status",
                    return_value={"screen_capture": True, "accessibility": True},
                ),
                patch(
                    "vntts.app.QTimer.singleShot",
                    side_effect=lambda _ms, *args: args[-1](),
                ),
            ):
                dialog.request_refresh()
                self.wait_until(lambda: controller.inspect_current_dialog.called)
                self.assertFalse(dialog.isVisible())

                tray_application.open_diagnostics()
                self.assertTrue(dialog.refresh_in_flight)
                self.assertFalse(dialog.isVisible())

                release.set()
                self.wait_until(lambda: not dialog.refresh_in_flight)
            self.assertEqual(dialog.text.text(), "Fresh capture")
            self.assertTrue(dialog.isVisible())
        finally:
            release.set()
            tray_application.shutdown()

    def test_diagnostic_result_restores_concealed_window(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        from vntts.diagnostics_ui import DiagnosticsDialog

        diagnostics_dialog = DiagnosticsDialog()
        tray_application.diagnostics_dialog = diagnostics_dialog
        snapshot = DiagnosticSnapshot(None, text="Visible after capture")
        try:
            for warning in (False, True):
                with self.subTest(warning=warning):
                    diagnostics_dialog.show()
                    diagnostics_dialog.conceal_for_capture()
                    if warning:
                        tray_application.set_diagnostics_error("Window unavailable")
                    else:
                        tray_application.update_diagnostics_snapshot(snapshot)
                        self.assertEqual(diagnostics_dialog.text.text(), snapshot.text)
                    self.assertTrue(diagnostics_dialog.isVisible())
                    self.assertFalse(diagnostics_dialog.concealed_for_capture)
        finally:
            tray_application.shutdown()
            diagnostics_dialog.deleteLater()

    def test_diagnostic_warning_routes_one_typed_recovery_action(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        diagnostics_dialog = Mock()
        tray_application.diagnostics_dialog = diagnostics_dialog
        remediation = ("settings", "Open Settings")

        with patch("vntts.app.diagnostic_remediation", return_value=remediation):
            tray_application.set_diagnostics_error("Window unavailable")

        diagnostics_dialog.set_warning.assert_called_once_with(
            "Window unavailable",
            remediation=remediation,
        )
        with patch.object(tray_application, "open_settings") as settings:
            tray_application._run_diagnostics_remediation("settings")
        settings.assert_called_once_with()
        tray_application.shutdown()

    def test_native_hotkey_callbacks_are_queued_on_the_qt_thread(self):
        read_threads, live_threads = [], []
        controller = Mock(is_live_running=False, live_reader=None)
        controller.read_once.side_effect = lambda: read_threads.append(get_ident())
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        try:
            with (
                patch("vntts.app.keyboard.GlobalHotKeys") as listener_factory,
                patch.object(
                    tray,
                    "_start_live_with_available_scope",
                    side_effect=lambda: live_threads.append(get_ident()),
                ),
            ):
                tray.start_hotkeys()
                callbacks = listener_factory.call_args.args[0]

                def activate():
                    callbacks[tray.settings.read_hotkey]()
                    callbacks[tray.settings.live_hotkey]()

                with patch.object(
                    tray,
                    "_runtime_control_state",
                    return_value=Mock(can_read=True, active=False, transition=None),
                ):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        executor.submit(activate).result(timeout=2)
                    self.assertEqual(read_threads, [])
                    self.assertEqual(live_threads, [])
                    self.wait_until(lambda: bool(read_threads and live_threads))
                    self.assertEqual(read_threads, [get_ident()])
                    self.assertEqual(live_threads, [get_ident()])

                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(activate).result(timeout=2)
                tray.shutdown()
                self.application.processEvents()
                self.assertEqual(read_threads, [get_ident()])
                self.assertEqual(live_threads, [get_ident()])
        finally:
            tray.shutdown()

    def test_hotkey_toggle_cannot_start_during_controller_reconfiguration(self):
        controller = Mock(is_live_running=False, live_reader=None)
        controller.toggle_live.return_value = True
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        try:
            tray._controller_busy = True
            self.assertFalse(tray.toggle_live())
            controller.toggle_live.assert_not_called()
            controller.unresolved_live_speakers.assert_not_called()
        finally:
            tray.shutdown()

    def test_invalid_hotkey_replacement_preserves_the_current_listener(self):
        tray = TrayApplication(
            self.application,
            AppSettings(read_hotkey="<ctrl>+h", live_hotkey="<ctrl>+h"),
            controller_factory=Mock(return_value=Mock()),
        )
        current = Mock()
        tray.hotkey_listener = current
        try:
            with self.assertRaises(ValueError):
                tray.start_hotkeys()
            current.stop.assert_not_called()
            self.assertIs(tray.hotkey_listener, current)
        finally:
            tray.shutdown()

    def test_invalid_saved_hotkey_falls_back_without_preventing_startup(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(read_hotkey="not a hotkey"),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.keyboard.GlobalHotKeys") as listener_factory:
            tray_application.start_hotkeys()

        registered_hotkeys = listener_factory.call_args.args[0]
        self.assertIn(AppSettings().read_hotkey, registered_hotkeys)
        self.assertEqual(
            set(registered_hotkeys),
            {
                AppSettings().read_hotkey,
                AppSettings().live_hotkey,
            },
        )
        listener_factory.return_value.start.assert_called_once_with()
        tray_application.shutdown()

    def test_failed_hotkey_start_preserves_the_current_listener_and_reports_error(self):
        tray = TrayApplication(
            self.application,
            AppSettings(read_hotkey="<ctrl>+h", live_hotkey="<ctrl>+l"),
            controller_factory=Mock(return_value=Mock()),
        )
        current = Mock()
        tray.hotkey_listener = current
        try:
            for error in (
                RuntimeError("thread unavailable"),
                OSError("listener unavailable"),
            ):
                for cleanup_error in (None, RuntimeError("cleanup unavailable")):
                    candidate = Mock()
                    candidate.start.side_effect = error
                    candidate.stop.side_effect = cleanup_error
                    with (
                        self.subTest(error=error, cleanup_error=cleanup_error),
                        patch("vntts.app.sys.platform", "win32"),
                        patch(
                            "vntts.app.keyboard.GlobalHotKeys", return_value=candidate
                        ),
                        patch.object(tray, "show_error") as show_error,
                    ):
                        tray._start_hotkeys_safely()
                        self.assertIs(tray.hotkey_listener, current)
                        current.stop.assert_not_called()
                        candidate.stop.assert_called_once_with()
                        show_error.assert_called_once_with(
                            f"Unable to register hotkeys: {error}"
                        )
                        if cleanup_error is not None:
                            self.assertIn(str(cleanup_error), error.__notes__[0])
        finally:
            tray.shutdown()

    def test_hotkey_replacement_starts_before_stopping_the_current_listener(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        current, candidate = Mock(), Mock()
        events = []
        candidate.start.side_effect = lambda: events.append("start")
        current.stop.side_effect = lambda: events.append("stop")
        tray.hotkey_listener = current
        try:
            with patch("vntts.app.keyboard.GlobalHotKeys", return_value=candidate):
                tray.start_hotkeys()
            self.assertEqual(events, ["start", "stop"])
            self.assertIs(tray.hotkey_listener, candidate)
        finally:
            tray.shutdown()

    def test_hotkey_start_preserves_fatal_start_error_when_cleanup_is_fatal(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        candidate = Mock()
        start_error = KeyboardInterrupt()
        candidate.start.side_effect = start_error
        candidate.stop.side_effect = SystemExit("cleanup interrupted")
        try:
            with (
                patch("vntts.app.sys.platform", "win32"),
                patch("vntts.app.keyboard.GlobalHotKeys", return_value=candidate),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                tray.start_hotkeys()
            self.assertIs(raised.exception, start_error)
            self.assertIn("Hotkey listener cleanup failed", start_error.__notes__[0])
            candidate.stop.assert_called_once_with()
        finally:
            tray.shutdown()

    def test_hotkey_registration_is_deferred_on_the_qt_thread(self):
        controller = Mock()
        controller.start.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.QTimer.singleShot") as single_shot:
            generation = tray_application._begin_controller_lifecycle()
            tray_application._initial_start_generation = generation
            ready = tray_application._initialize_controller(generation)
            tray_application._initial_start_finished(ready, None)

        single_shot.assert_called_once_with(
            250,
            tray_application,
            tray_application._start_hotkeys_safely,
        )
        tray_application.shutdown()

    def test_controller_lifecycle_shows_main_window_loading_state(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )

        tray_application._begin_controller_lifecycle()

        self.assertFalse(tray_application.dashboard.loading_panel.isHidden())
        self.assertFalse(tray_application.dashboard.prepare_audio_button.isEnabled())
        self.assertFalse(tray_application.pregeneration_action.isEnabled())
        for button in (
            tray_application.dashboard.live_button,
            tray_application.dashboard.read_button,
        ):
            self.assertFalse(button.isEnabled())
        self.assertIn(
            "unlock automatically", tray_application.dashboard.action_reason.text()
        )

        tray_application._finish_controller_lifecycle()

        self.assertTrue(tray_application.dashboard.loading_panel.isHidden())
        self.assertTrue(tray_application.dashboard.prepare_audio_button.isEnabled())
        self.assertTrue(tray_application.pregeneration_action.isEnabled())
        tray_application.shutdown()

    def test_stopped_reading_clears_paused_ui_before_restart(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.set_live(True)
        tray_application.set_speech_paused(True)
        self.assertEqual(tray_application.dashboard.live_button.text(), "Stop reading")

        tray_application.set_live(False)
        self.assertEqual(tray_application.dashboard.live_button.text(), "Start reading")
        tray_application.set_live(True)
        self.assertEqual(tray_application.dashboard.live_button.text(), "Stop reading")
        tray_application.shutdown()

    def test_quit_during_initial_start_forces_late_controller_cleanup(self):
        started = Event()
        release = Event()
        runtime = {"live": False}
        controller = Mock()

        def start():
            started.set()
            release.wait(2)
            runtime["live"] = True
            return True

        def shutdown():
            runtime["live"] = False

        controller.start.side_effect = start
        controller.shutdown.side_effect = shutdown
        controller.request_shutdown.side_effect = release.set
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=True),
            controller_factory=Mock(return_value=controller),
        )
        ready_events = []
        hotkey_events = []
        tray_application.signals.ready_changed.connect(ready_events.append)
        tray_application.signals.hotkeys_requested.connect(
            lambda: hotkey_events.append(True)
        )

        with (
            patch.object(tray_application, "show_dashboard"),
            patch.object(tray_application, "show_compact_controls"),
            patch.object(tray_application.tray, "show"),
        ):
            tray_application.prepare_reading()
            self.assertTrue(started.wait(1))
            tray_application.shutdown()
            self.wait_until(lambda: controller.shutdown.call_count == 1)

        self.assertFalse(runtime["live"])
        self.assertEqual(ready_events, [])
        self.assertEqual(hotkey_events, [])
        self.assertTrue(tray_application._shutting_down)
        controller.request_shutdown.assert_called_once_with()

    def test_macos_skips_unstable_native_hotkey_listener(self):
        controller = Mock()
        controller.get_capture_geometry.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch("vntts.app.sys.platform", "darwin"),
            patch.object(tray_application, "start_hotkeys") as start_hotkeys,
        ):
            tray_application._start_hotkeys_safely()

        start_hotkeys.assert_not_called()
        self.assertTrue(tray_application.compact_controller.isVisible())
        self.assertFalse(tray_application.dashboard.isVisible())
        self.assertIn("macOS hotkeys disabled", tray_application.status_action.text())
        self.assertTrue(
            any(
                "Compact controls were opened" in entry["message"]
                for entry in tray_application.support_log.snapshot()
            )
        )
        tray_application.shutdown()

    def test_window_calibration_uses_selected_client_geometry(self):
        controller = Mock()
        geometry = WindowGeometry(100, 200, 1600, 900)
        controller.get_capture_geometry.return_value = geometry
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
            ),
            controller_factory=Mock(return_value=controller),
        )
        background = object()

        with (
            patch("vntts.app.show_calibration_overlay") as show_overlay,
            patch(
                "vntts.app.capture_calibration_background",
                return_value=background,
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: args[-1](),
            ),
        ):
            tray_application.calibrate()
            self.wait_until(lambda: show_overlay.called)

        show_overlay.assert_called_once_with(
            geometry,
            background=background,
            save_region=tray_application._save_calibration_region,
        )
        tray_application.shutdown()

    def test_calibration_overlay_blocks_reentry_until_it_closes(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock(is_live_running=False)),
        )
        overlay = Mock()
        try:
            with (
                patch("vntts.app.show_calibration_overlay", return_value=overlay),
                patch("vntts.app.QTimer.singleShot") as schedule_capture,
                patch.object(tray, "restore_control_window") as restore,
            ):
                tray._open_calibration_overlay(None, Mock())
                close_overlay = overlay.closed.connect.call_args.args[0]

                tray.calibrate()
                schedule_capture.assert_not_called()

                close_overlay()
                self.assertIsNone(tray.calibration_overlay)
                overlay.deleteLater.assert_called_once_with()
                restore.assert_called_once_with()

                tray.calibrate()
                schedule_capture.assert_called_once_with(
                    200, tray, tray._start_calibration_capture
                )
        finally:
            tray.shutdown()

    def test_shutdown_closes_calibration_overlay_without_restoring_controls(self):
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        overlay = Mock()
        try:
            with (
                patch("vntts.app.show_calibration_overlay", return_value=overlay),
                patch.object(tray, "restore_control_window") as restore,
            ):
                tray._open_calibration_overlay(None, Mock())
                close_overlay = overlay.closed.connect.call_args.args[0]
                overlay.close.side_effect = close_overlay

                tray.shutdown()

                overlay.close.assert_called_once_with()
                overlay.deleteLater.assert_called_once_with()
                self.assertIsNone(tray.calibration_overlay)
                restore.assert_not_called()
        finally:
            tray.shutdown()

    def test_slow_calibration_capture_keeps_ui_responsive(self):
        started = Event()
        release = Event()
        heartbeat = []
        controller = Mock(is_live_running=False)
        controller.get_capture_geometry.return_value = None
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        def slow_capture(_geometry):
            started.set()
            release.wait(2)
            return object()

        with (
            patch("vntts.app.capture_calibration_background", side_effect=slow_capture),
            patch("vntts.app.show_calibration_overlay") as show_overlay,
        ):
            try:
                tray.calibrate()
                self.wait_until(started.is_set)
                self.assertTrue(tray.calibration_capture_runner.active)
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.wait_until(lambda: bool(heartbeat))
                show_overlay.assert_not_called()
            finally:
                release.set()
            self.wait_until(lambda: show_overlay.called)

        tray.shutdown()

    def test_diagnostics_region_button_shows_pending_live_stop(self):
        release = Event()
        controller = Mock(is_live_running=True)
        controller.get_latest_diagnostic.return_value = None

        def stop_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = stop_live
        controller.live_reader.wait.side_effect = lambda **_kwargs: release.wait(2)
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray.open_diagnostics()
        dialog = tray.diagnostics_dialog

        with (
            patch.object(
                tray, "_capture_calibration_background", return_value=(None, object())
            ),
            patch.object(tray, "_open_calibration_overlay") as overlay,
        ):
            dialog.calibrate_button.click()
            self.assertFalse(dialog.calibrate_button.isEnabled())
            self.assertEqual(dialog.calibrate_button.text(), "Stopping reading...")
            release.set()
            self.wait_until(lambda: overlay.called)

        self.assertFalse(dialog.isVisible())
        tray.shutdown()

    def test_diagnostics_region_stop_timeout_allows_retry(self):
        controller = Mock(is_live_running=True)
        controller.get_latest_diagnostic.return_value = None
        controller.toggle_live.side_effect = lambda: False
        controller.live_reader.wait.side_effect = TimeoutError("reader stuck")
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray.open_diagnostics()
        dialog = tray.diagnostics_dialog

        dialog.calibrate_button.click()
        self.wait_until(lambda: not tray.live_stop_runner.active)

        self.assertTrue(dialog.calibrate_button.isEnabled())
        self.assertIn("reader stuck", dialog.warning.text())
        tray.shutdown()

    def test_diagnostics_can_change_region_and_refresh_after_calibration(self):
        controller = Mock()
        controller.get_latest_diagnostic.return_value = None
        geometry = WindowGeometry(100, 200, 1600, 900)
        controller.get_capture_geometry.return_value = geometry
        tray_application = TrayApplication(
            self.application,
            AppSettings(capture_mode="window"),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.open_diagnostics()
        dialog = tray_application.diagnostics_dialog
        self.assertTrue(dialog.calibrate_button.isVisible())
        self.assertEqual(
            dialog.calibrate_button.accessibleName(),
            "Change captured dialogue region",
        )
        overlay = Mock()
        background = object()

        with (
            patch("vntts.app.show_calibration_overlay", return_value=overlay) as show,
            patch(
                "vntts.app.capture_calibration_background",
                return_value=background,
            ),
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, *args: args[-1](),
            ),
            patch.object(dialog, "request_refresh") as refresh,
        ):
            dialog.calibrate_button.click()
            self.assertFalse(dialog.isVisible())
            self.wait_until(lambda: show.called)
            show.assert_called_once_with(
                geometry,
                background=background,
                save_region=tray_application._save_calibration_region,
            )

            overlay.closed.connect.call_args.args[0]()

            self.assertTrue(dialog.isVisible())
            refresh.assert_called_once_with()

        tray_application.shutdown()

    def test_calibration_updates_the_active_game_profile(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Game", AppSettings())
            tray_application = TrayApplication(
                self.application,
                AppSettings(active_profile_id=profile.id),
                controller_factory=Mock(return_value=Mock()),
                profile_store=store,
            )
            region = DialogRegion(0.1, 0.6, 0.8, 0.3)

            tray_application._save_calibration_region(region)

            self.assertEqual(store.get(profile.id).dialog_region, region)
            tray_application.shutdown()

    def test_calibration_profile_save_failure_keeps_previous_region(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Game", AppSettings())
            tray_application = TrayApplication(
                self.application,
                AppSettings(active_profile_id=profile.id),
                controller_factory=Mock(return_value=Mock()),
                profile_store=store,
            )
            region = DialogRegion(0.1, 0.6, 0.8, 0.3)
            with patch.object(store, "update_region", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    tray_application._save_calibration_region(region)

            self.assertEqual(store.get(profile.id).dialog_region, profile.dialog_region)
            tray_application.shutdown()

    def test_profile_selection_reloads_runtime_with_profile_settings(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create(
                "Reverse: 1999",
                AppSettings(game_window_title="Reverse: 1999"),
            )
            selected_settings = profile.apply(AppSettings())
            controller = Mock()
            controller.start.return_value = True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch.object(tray_application, "start_hotkeys"),
                patch(
                    "vntts.app.TrayApplication._save_settings_candidate",
                    return_value=Path("settings.json"),
                ),
                patch("vntts.app.save_dialog_region") as save_region,
            ):
                tray_application.open_profiles()
                self.wait_until(
                    lambda: not tray_application.profile_restart_runner.active
                )

            controller.shutdown.assert_called_once_with()
            save_region.assert_not_called()
            controller.apply_settings.assert_called_once_with(
                selected_settings, cancellation=ANY
            )
            controller.start.assert_called_once_with()
            self.assertIn("Reverse: 1999", tray_application.status_action.text())
            tray_application.shutdown()

    def test_failed_settings_write_keeps_previous_profile_active(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Game", AppSettings())
            original = AppSettings()
            selected = profile.apply(original)
            tray_application = TrayApplication(
                self.application,
                original,
                controller_factory=Mock(return_value=Mock()),
                profile_store=store,
            )
            dialog = Mock()
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.settings.return_value = selected
            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch(
                    "vntts.app.TrayApplication._save_settings_candidate",
                    side_effect=OSError("disk full"),
                ),
            ):
                tray_application.open_profiles()

            self.assertIs(tray_application.settings, original)
            self.assertFalse(tray_application.profile_restart_runner.active)
            self.assertIn("disk full", tray_application.status_action.text())
            tray_application.shutdown()

    def test_profile_manager_repairs_saved_profile_without_persisting_environment_override(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store_path = root / "profiles.json"
            settings_path = root / "settings.json"
            store = GameProfileStore(store_path)
            profile = store.create("Game", AppSettings(game_window_title="Old"))
            profile.apply(AppSettings()).updated(game_window_title="Saved").save(
                settings_path
            )
            runtime = load_app_settings(
                settings_path,
                environment={"VNTTS_GAME_WINDOW_TITLE": "Temporary"},
            )
            restored_store = GameProfileStore.load(store_path)
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(settings_path)}):
                tray = TrayApplication(
                    self.application,
                    runtime,
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=restored_store,
                )
                with patch("vntts.app.GameProfilesDialog") as dialog:
                    dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
                    tray.open_profiles()

            self.assertEqual(tray.settings.game_window_title, "Temporary")
            self.assertEqual(
                GameProfileStore.load(store_path).get(profile.id).game_window_title,
                "Saved",
            )
            tray.shutdown()

    def test_profile_manager_stays_closed_when_profile_recovery_fails(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store_path = root / "profiles.json"
            settings_path = root / "settings.json"
            store = GameProfileStore(store_path)
            profile = store.create("Game", AppSettings(game_window_title="Old"))
            saved = profile.apply(AppSettings()).updated(game_window_title="Saved")
            saved.save(settings_path)
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(settings_path)}):
                tray = TrayApplication(
                    self.application,
                    load_app_settings(settings_path, environment={}),
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=GameProfileStore.load(store_path),
                )
                with (
                    patch.object(
                        tray.profile_store,
                        "update_from_settings",
                        side_effect=OSError("disk full"),
                    ),
                    patch("vntts.app.GameProfilesDialog") as dialog,
                ):
                    tray.open_profiles()

            dialog.assert_not_called()
            self.assertEqual(tray.settings.game_window_title, "Saved")
            self.assertEqual(
                GameProfileStore.load(store_path).get(profile.id).game_window_title,
                "Old",
            )
            self.assertIn("disk full", tray.status_action.text())
            tray.shutdown()

    def test_profile_recovery_preserves_changes_from_another_store(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store_path = root / "profiles.json"
            settings_path = root / "settings.json"
            store = GameProfileStore(store_path)
            profile = store.create("Game", AppSettings(game_window_title="Old"))
            profile.apply(AppSettings()).updated(game_window_title="Saved").save(
                settings_path
            )
            stale = GameProfileStore.load(store_path)
            GameProfileStore.load(store_path).rename(profile.id, "Externally renamed")
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(settings_path)}):
                tray = TrayApplication(
                    self.application,
                    load_app_settings(settings_path, environment={}),
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=stale,
                )
                with patch("vntts.app.GameProfilesDialog") as dialog:
                    tray.open_profiles()

            dialog.assert_not_called()
            self.assertEqual(
                GameProfileStore.load(store_path).get(profile.id).name,
                "Externally renamed",
            )
            self.assertIn(
                "active profile could not be updated", tray.status_action.text()
            )
            tray.shutdown()

    def test_live_modal_stop_wait_does_not_block_qt_events(self):
        controller = Mock()
        controller.is_live_running = True
        release = Event()

        def toggle_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = toggle_live
        controller.live_reader.wait.side_effect = lambda **_kwargs: release.wait(2)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        opened = []
        heartbeat = []

        with patch.object(
            tray_application,
            "_open_history_dialog",
            side_effect=lambda resume: opened.append(resume),
        ):
            tray_application.open_history()
            QTimer.singleShot(0, lambda: heartbeat.append(True))
            self.application.processEvents()
            self.assertEqual(heartbeat, [True])
            self.assertEqual(opened, [])
            self.assertTrue(tray_application.live_stop_runner.active)
            release.set()
            self.wait_until(lambda: opened == [True])

        tray_application.shutdown()

    def test_live_modal_stop_timeout_restores_actions_for_retry(self):
        controller = Mock()
        controller.is_live_running = True

        def toggle_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = toggle_live
        controller.live_reader.wait.side_effect = TimeoutError("reader stuck")
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_stop_runner.thread_pool = Mock(
            start=lambda task: task.run()
        )
        tray_application.set_ready(True)

        with patch.object(tray_application, "_open_history_dialog") as opened:
            tray_application.open_history()
            self.wait_until(lambda: not tray_application.live_stop_runner.active)

        controller.live_reader.wait.assert_called_once_with(timeout_seconds=5.0)
        opened.assert_not_called()
        self.assertTrue(tray_application.history_action.isEnabled())
        self.assertIn(
            "Unable to stop live capture", tray_application.status_action.text()
        )
        tray_application.shutdown()

    def test_profile_restart_does_not_block_qt_events(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            release = Event()
            controller = Mock()
            controller.start.side_effect = lambda: release.wait(2) or True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings
            heartbeat = []

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch(
                    "vntts.app.TrayApplication._save_settings_candidate",
                    return_value=Path("settings.json"),
                ),
                patch("vntts.app.save_dialog_region"),
            ):
                tray_application.open_profiles()
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.application.processEvents()
                self.assertEqual(heartbeat, [True])
                self.assertTrue(tray_application.profile_restart_runner.active)
                self.assertFalse(tray_application.profiles_action.isEnabled())
                release.set()
                self.wait_until(
                    lambda: not tray_application.profile_restart_runner.active
                )

            self.assertTrue(tray_application.profiles_action.isEnabled())
            tray_application.shutdown()

    def test_settings_and_assets_apply_without_blocking_qt_events(self):
        original = AppSettings()
        candidate = original.updated(game_window_title="Changed")
        for dialog_name, method_name in (
            ("SettingsDialog", "open_settings"),
            ("AssetManagerDialog", "open_assets"),
        ):
            with self.subTest(method=method_name):
                started = Event()
                release = Event()
                controller = Mock(settings=original)

                def blocked_apply(_settings, **_options):
                    started.set()
                    release.wait(2)

                controller.apply_settings.side_effect = blocked_apply
                tray_application = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                dialog = Mock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.settings.return_value = candidate
                heartbeat = []
                with (
                    patch(f"vntts.app.{dialog_name}", return_value=dialog),
                    patch(
                        "vntts.app.TrayApplication._save_settings_candidate",
                        return_value=Path("settings.json"),
                    ),
                ):
                    getattr(tray_application, method_name)()
                    QTimer.singleShot(0, lambda: heartbeat.append(True))
                    self.wait_until(lambda: started.is_set() and bool(heartbeat))
                    self.assertTrue(tray_application.configuration_runner.active)
                    self.assertFalse(tray_application.settings_action.isEnabled())
                    self.assertFalse(tray_application.assets_action.isEnabled())
                    release.set()
                    self.wait_until(
                        lambda: not tray_application.configuration_runner.active
                    )

                controller.apply_settings.assert_called_once_with(
                    candidate,
                    cancellation=ANY,
                )
                self.assertTrue(tray_application.settings_action.isEnabled())
                tray_application.shutdown()

    def test_saved_settings_runtime_apply_can_be_cancelled(self):
        original = AppSettings()
        candidate = original.updated(game_window_title="Changed")
        started = Event()
        controller = Mock(settings=original)

        def blocked_apply(_settings, *, cancellation):
            started.set()
            cancellation.wait(2)
            return False

        def cancel(cancellation):
            cancellation.set()
            return True

        controller.apply_settings.side_effect = blocked_apply
        controller.cancel_settings_apply.side_effect = cancel
        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.settings.return_value = candidate
        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch(
                "vntts.app.TrayApplication._save_settings_candidate",
                return_value=Path("settings.json"),
            ),
        ):
            tray_application.open_settings()
            self.wait_until(started.is_set)
            self.assertTrue(tray_application.cancel_configuration_action.isVisible())
            self.assertTrue(tray_application.cancel_configuration_action.isEnabled())
            tray_application.cancel_configuration_action.trigger()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        cancellation = controller.apply_settings.call_args.kwargs["cancellation"]
        controller.cancel_settings_apply.assert_called_once_with(cancellation)
        self.assertIs(tray_application.settings, candidate)
        self.assertIn("saved settings", tray_application.status_action.text().lower())
        self.assertFalse(tray_application.cancel_configuration_action.isVisible())
        tray_application.shutdown()

    def test_profile_restart_disables_runtime_and_quit_prevents_restart(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            entered = Event()
            release = Event()
            controller = Mock()

            def blocked_shutdown():
                entered.set()
                release.wait(2)

            controller.shutdown.side_effect = blocked_shutdown
            controller.start.return_value = True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            tray_application.set_ready(True)
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch(
                    "vntts.app.TrayApplication._save_settings_candidate",
                    return_value=Path("settings.json"),
                ),
                patch("vntts.app.save_dialog_region"),
            ):
                tray_application.open_profiles()
                self.assertTrue(entered.wait(1))
                self.application.processEvents()
                self.assertFalse(tray_application.read_action.isEnabled())
                self.assertFalse(tray_application.live_action.isEnabled())
                self.assertFalse(tray_application.dashboard.read_button.isEnabled())
                tray_application.shutdown()
                release.set()
                tray_application.profile_restart_runner.thread_pool.waitForDone(2_000)
                self.application.processEvents()

            controller.start.assert_not_called()
            controller.apply_settings.assert_not_called()
            self.assertFalse(tray_application.read_action.isEnabled())
            self.assertFalse(tray_application.history_action.isEnabled())

    def test_quit_during_profile_start_cleans_up_the_late_runtime(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            entered = Event()
            release = Event()
            controller = Mock()

            def blocked_start():
                entered.set()
                release.wait(2)
                return True

            controller.start.side_effect = blocked_start
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            tray_application.set_ready(True)
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch(
                    "vntts.app.TrayApplication._save_settings_candidate",
                    return_value=Path("settings.json"),
                ),
                patch("vntts.app.save_dialog_region"),
            ):
                tray_application.open_profiles()
                self.assertTrue(entered.wait(1))
                tray_application.shutdown()
                release.set()
                tray_application.profile_restart_runner.thread_pool.waitForDone(2_000)
                self.application.processEvents()

            controller.start.assert_called_once_with()
            self.assertEqual(controller.shutdown.call_count, 2)
            self.assertFalse(tray_application.read_action.isEnabled())

    def test_shutdown_cancels_a_pending_live_stop_continuation(self):
        controller = Mock()
        controller.is_live_running = True
        release = Event()

        def toggle_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = toggle_live
        controller.live_reader.wait.side_effect = lambda **_kwargs: release.wait(2)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.set_ready(True)

        with patch.object(tray_application, "_open_history_dialog") as opened:
            tray_application.open_history()
            self.assertTrue(tray_application.live_stop_runner.active)
            self.assertFalse(tray_application.history_action.isEnabled())
            tray_application.shutdown()
            release.set()
            tray_application.live_stop_runner.thread_pool.waitForDone(2_000)
            self.application.processEvents()

        opened.assert_not_called()
        self.assertFalse(tray_application.history_action.isEnabled())

    def test_main_section_restores_without_playback_and_survives_lazy_library_loading(
        self,
    ):
        for section, index in (("voices", 1), ("reading", 2)):
            with self.subTest(section=section), TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                controller = Mock(is_live_running=False)
                settings = AppSettings(last_main_section=section)
                settings.save(path)
                with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                    tray = TrayApplication(
                        self.application,
                        settings,
                        controller_factory=Mock(return_value=controller),
                    )
                    with (
                        patch.object(tray.tray, "show"),
                        patch("vntts.app.QTimer.singleShot"),
                        patch.object(tray, "_save_settings_candidate") as save,
                    ):
                        tray.start()
                        self.assertEqual(tray.dashboard.sections.currentIndex(), index)
                        with patch.object(
                            tray,
                            "open_pregeneration",
                            side_effect=tray.dashboard.show_stories,
                        ) as load_library:
                            tray._load_initial_library()
                        load_library.assert_called_once()
                        save.assert_not_called()
                    self.assertEqual(tray.dashboard.sections.currentIndex(), index)
                    self.assertEqual(tray.settings.last_main_section, section)
                    self.assertIsNone(tray.onboarding_wizard)
                    controller.start.assert_not_called()
                    controller.toggle_live.assert_not_called()
                    tray.shutdown()

    def test_main_section_navigation_saves_and_reports_write_failure(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                tray = TrayApplication(
                    self.application,
                    AppSettings(),
                    controller_factory=Mock(return_value=Mock()),
                )
                tray.dashboard.show()
                QTest.keyClick(tray.dashboard.sections.tabBar(), Qt.Key.Key_Right)
                self.assertEqual(tray.settings.last_main_section, "voices")
                self.assertIn('"last_main_section": "voices"', path.read_text())
                with (
                    patch.object(
                        tray,
                        "_save_settings_candidate",
                        side_effect=OSError("disk full"),
                    ),
                    patch.object(tray, "show_error") as error,
                ):
                    QTest.keyClick(tray.dashboard.sections.tabBar(), Qt.Key.Key_Right)
                self.assertEqual(tray.dashboard.sections.currentIndex(), 2)
                self.assertEqual(tray.settings.last_main_section, "voices")
                self.assertIn("disk full", error.call_args.args[0])
                tray.shutdown()

    def test_fallback_settings_cannot_replace_future_document(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"schema_version": settings_schema_version + 1}),
                encoding="utf-8",
            )
            original = path.read_bytes()
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                tray = TrayApplication(
                    self.application,
                    controller_factory=Mock(return_value=Mock()),
                )
                with self.assertRaisesRegex(OSError, "changed on disk"):
                    tray._save_settings_candidate(tray.settings)
                tray.shutdown()
            self.assertEqual(path.read_bytes(), original)

    def test_loaded_settings_and_save_baseline_share_the_exact_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            AppSettings(game_window_title="Original game").save(path)
            original = path.read_bytes()
            AppSettings(game_window_title="Intervening game").save(path)
            intervening = path.read_bytes()
            path.write_bytes(original)

            def read_intervening_snapshot(source, **kwargs):
                if Path(source) != path:
                    return read_versioned_json_snapshot(source, **kwargs)
                path.write_bytes(intervening)
                snapshot = read_versioned_json_snapshot(source, **kwargs)
                path.write_bytes(original)
                return snapshot

            with (
                patch.dict(
                    os.environ,
                    {
                        "VNTTS_SETTINGS_FILE": str(path),
                        "VNTTS_GAME_WINDOW_TITLE": "Temporary game",
                    },
                ),
                patch(
                    "vntts.versioned_json.read_versioned_json_snapshot",
                    side_effect=read_intervening_snapshot,
                ) as read_snapshot,
            ):
                tray = TrayApplication(
                    self.application,
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=GameProfileStore(Path(directory) / "profiles.json"),
                )
                self.addCleanup(tray.shutdown)
                read_snapshot.assert_called_once()
                self.assertEqual(tray.settings.game_window_title, "Temporary game")
                self.assertEqual(
                    tray._last_saved_settings.game_window_title, "Intervening game"
                )
                with self.assertRaisesRegex(OSError, "changed on disk"):
                    tray._save_settings_candidate(
                        tray.settings.updated(compact_controls=True)
                    )
            self.assertEqual(path.read_bytes(), original)

    def test_second_application_cannot_replace_newer_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = AppSettings()
            original.save(path)
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                first = TrayApplication(
                    self.application,
                    load_app_settings(path, environment={}),
                    controller_factory=Mock(return_value=Mock()),
                )
                second = TrayApplication(
                    self.application,
                    load_app_settings(path, environment={}),
                    controller_factory=Mock(return_value=Mock()),
                )
                first._save_compact_preference(True)
                first._save_main_section("reading")
                second._save_main_section("voices")

            saved = load_app_settings(path, environment={})
            self.assertTrue(saved.compact_controls)
            self.assertEqual(saved.last_main_section, "reading")
            self.assertEqual(first.settings, saved)
            self.assertEqual(second.settings, original)
            self.assertIn("changed on disk", second.status_action.toolTip())
            first.shutdown()
            second.shutdown()

    def test_unrelated_preference_save_does_not_persist_environment_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            AppSettings(game_window_title="Saved game").save(path)
            with patch.dict(
                os.environ,
                {
                    "VNTTS_SETTINGS_FILE": str(path),
                    "VNTTS_GAME_WINDOW_TITLE": "Temporary game",
                },
            ):
                tray = TrayApplication(
                    self.application,
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=GameProfileStore(Path(directory) / "profiles.json"),
                )
                self.assertEqual(tray.settings.game_window_title, "Temporary game")
                tray._save_compact_preference(True)
                self.assertEqual(
                    load_app_settings(path, environment={}).game_window_title,
                    "Saved game",
                )
                chosen = tray.settings.updated(game_window_title="Chosen game")
                tray._save_settings_candidate(chosen)
                tray.settings = chosen
                tray._save_main_section("reading")
                tray.shutdown()

            saved = load_app_settings(path, environment={})
            self.assertEqual(saved.game_window_title, "Chosen game")
            self.assertTrue(saved.compact_controls)
            self.assertEqual(saved.last_main_section, "reading")

    def test_profile_sync_uses_saved_settings_without_environment_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = GameProfileStore(Path(directory) / "profiles.json")
            profile = store.create("Game", AppSettings(game_window_title="Saved game"))
            AppSettings(
                active_profile_id=profile.id, game_window_title="Saved game"
            ).save(path)
            with patch.dict(
                os.environ,
                {
                    "VNTTS_SETTINGS_FILE": str(path),
                    "VNTTS_GAME_WINDOW_TITLE": "Temporary game",
                },
            ):
                tray = TrayApplication(
                    self.application,
                    controller_factory=Mock(return_value=Mock()),
                    profile_store=store,
                )
                tray._save_compact_preference(True)
                tray._sync_active_profile()
                self.assertEqual(store.get(profile.id).game_window_title, "Saved game")
                tray.shutdown()

    def test_offline_pack_activator_uses_settings_revision_guard(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = AppSettings()
            original.save(path)
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                tray = TrayApplication(
                    self.application,
                    load_app_settings(path, environment={}),
                    controller_factory=Mock(return_value=Mock()),
                )
                external = original.updated(output_volume_percent=42)
                external.save(path)
                with self.assertRaisesRegex(OSError, "changed on disk"):
                    tray.pregeneration_activator.save_settings(
                        original.updated(audio_source_policy="prefer-generated")
                    )

            self.assertEqual(load_app_settings(path, environment={}), external)
            self.assertEqual(tray.settings, original)
            tray.shutdown()

    def test_pack_activation_keeps_reading_section_when_worker_saved_an_old_tab(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                original = AppSettings()
                controller = Mock(is_ready=False, is_live_running=False)
                tray = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                tray._pregeneration_activation_generation = (
                    tray._begin_controller_lifecycle()
                )
                tray.dashboard.show_reading()
                candidate = original.updated(audio_source_policy="prefer-generated")
                tray._save_settings_candidate(candidate)

                tray._pregeneration_activation_finished(
                    OfflinePackActivationResult(candidate, path, False), None
                )

                self.assertEqual(tray.settings.last_main_section, "reading")
                self.assertEqual(tray.settings.audio_source_policy, "prefer-generated")
                self.assertIn('"last_main_section": "reading"', path.read_text())
                self.assertIs(
                    tray.dashboard.focusWidget(), tray.dashboard.prepare_reading_button
                )
                self.assertIn("click Set up reading", tray.status_action.text())
                controller.toggle_live.assert_not_called()
                tray.shutdown()

    def test_pack_activation_focuses_start_reading_when_controller_is_ready(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.dict(os.environ, {"VNTTS_SETTINGS_FILE": str(path)}):
                original = AppSettings()
                controller = Mock(is_ready=True, is_live_running=False)
                tray = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                tray._pregeneration_activation_generation = (
                    tray._begin_controller_lifecycle()
                )
                candidate = original.updated(audio_source_policy="prefer-generated")
                tray._save_settings_candidate(candidate)

                tray._pregeneration_activation_finished(
                    OfflinePackActivationResult(candidate, path, False), None
                )

                self.assertIs(tray.dashboard.focusWidget(), tray.dashboard.live_button)
                self.assertIn("click Start reading", tray.status_action.text())
                controller.toggle_live.assert_not_called()
                tray.shutdown()

    def test_incomplete_setup_opens_stories_without_loading_model(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch.object(tray_application.tray, "show"),
            patch("vntts.app.QTimer.singleShot") as single_shot,
        ):
            tray_application.start()

        controller.start.assert_not_called()
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertFalse(tray_application.compact_controller.isVisible())
        self.assertEqual(single_shot.call_args.args[0], 0)
        self.assertIs(single_shot.call_args.args[1], tray_application)
        self.assertEqual(
            single_shot.call_args.args[2], tray_application._load_initial_library
        )
        tray_application.shutdown()

    def test_first_launch_defers_setup_until_reading_is_requested(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=Mock()),
        )

        with (
            patch.object(tray_application.tray, "show"),
            patch("vntts.app.QTimer.singleShot"),
        ):
            tray_application.start()
            self.assertIsNone(tray_application.onboarding_wizard)
            tray_application.prepare_reading()

        wizard = tray_application.onboarding_wizard
        self.assertIsNotNone(wizard)
        self.assertTrue(wizard.isVisible())
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertFalse(tray_application.compact_controller.isVisible())

        wizard.reject()
        self.application.processEvents()

        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertIsNone(tray_application.onboarding_wizard)
        tray_application.shutdown()

    def test_onboarding_wizard_runs_without_nested_modal_event_loop(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=Mock()),
        )

        tray_application.run_onboarding()
        wizard = tray_application.onboarding_wizard

        self.assertIsNotNone(wizard)
        self.assertTrue(wizard.isVisible())

        wizard.reject()
        self.application.processEvents()

        self.assertIsNone(tray_application.onboarding_wizard)
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertEqual(tray_application.status_action.text(), "Setup required")
        tray_application.shutdown()

    def test_onboarding_voice_editor_returns_to_unsaved_game_window(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.run_onboarding()
        wizard = tray_application.onboarding_wizard
        wizard.configuration_page.game_window.setCurrentText("Manually chosen")

        with patch.object(tray_application, "open_voice_previews") as open_voices:
            wizard.request_voices()

        open_voices.assert_called_once_with()
        self.assertIs(tray_application.onboarding_wizard, wizard)
        self.assertTrue(wizard.isVisible())
        self.assertEqual(
            wizard.configuration_page.game_window.currentText(), "Manually chosen"
        )
        self.assertEqual(wizard.draft_settings.game_window_title, "Manually chosen")
        wizard.reject()
        self.application.processEvents()
        tray_application.shutdown()

    def test_successful_onboarding_returns_to_focused_start_action_without_playing(
        self,
    ):
        controller = Mock()
        controller.is_ready = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.run_onboarding()
        wizard = tray_application.onboarding_wizard
        wizard.test_page.set_result(True, "Success. Recognized Rhiannon: Hello.")

        with patch(
            "vntts.app.TrayApplication._save_settings_candidate",
            return_value=Path("settings.json"),
        ):
            wizard.accept()
            self.application.processEvents()

        self.assertIsNone(tray_application.onboarding_wizard)
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertIs(
            tray_application.dashboard.focusWidget(),
            tray_application.dashboard.live_button,
        )
        self.assertIn(
            "Click Start reading when ready", tray_application.status_action.text()
        )
        controller.toggle_live.assert_not_called()
        controller.apply_settings.assert_not_called()
        tray_application.shutdown()

    def test_onboarding_test_runs_controller_end_to_end(self):
        controller = Mock()
        controller.start.return_value = True
        controller.test_current_dialog.return_value = (
            "Marcus",
            "This is a complete test.",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        tray_application.onboarding_test_runner.thread_pool = ImmediateTaskPool()
        tray_application.run_onboarding_test(
            AppSettings(speech_backend="coqui-xtts", tts_model="xtts_v2")
        )

        controller.apply_settings.assert_called_once()
        controller.model_assets.download.assert_called_once()
        controller.test_current_dialog.assert_called_once_with()
        controller.shutdown.assert_not_called()
        self.assertTrue(results[0][0])
        self.assertIn("Marcus", results[0][1])
        tray_application.shutdown()

    def test_onboarding_test_requires_a_coqui_model(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )
        tray_application.run_onboarding_test(
            AppSettings(speech_backend="coqui-xtts", tts_model=None)
        )
        self.assertEqual(
            results, [(False, "Select a Coqui model before testing speech.")]
        )
        controller.model_assets.download.assert_not_called()
        controller.start.assert_not_called()
        tray_application.shutdown()

    def test_onboarding_test_shuts_down_after_preview_error(self):
        controller = Mock()
        controller.start.return_value = True
        controller.test_current_dialog.side_effect = RuntimeError("preview failed")
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        tray_application.onboarding_test_runner.thread_pool = ImmediateTaskPool()
        tray_application.run_onboarding_test(AppSettings())

        controller.shutdown.assert_called_once_with()
        self.assertEqual(
            results,
            [(False, "Unexpected dialog processing failure: preview failed")],
        )
        tray_application.shutdown()

    def test_onboarding_preview_failure_waits_for_controller_cleanup(self):
        cleanup_entered = Event()
        release_cleanup = Event()
        controller = Mock()
        controller.start.return_value = True
        controller.test_current_dialog.side_effect = RuntimeError("preview failed")

        def blocked_shutdown():
            cleanup_entered.set()
            release_cleanup.wait(2)

        controller.shutdown.side_effect = blocked_shutdown
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )
        try:
            tray.run_onboarding_test(AppSettings())
            self.wait_until(cleanup_entered.is_set)

            self.assertEqual(results, [])
            self.assertTrue(tray._controller_busy)
            self.assertTrue(tray.onboarding_test_runner.active)

            release_cleanup.set()
            self.wait_until(
                lambda: bool(results) and not tray.onboarding_test_runner.active
            )

            self.assertEqual(
                results,
                [(False, "Unexpected dialog processing failure: preview failed")],
            )
            self.assertFalse(tray._controller_busy)
        finally:
            release_cleanup.set()
            tray.shutdown()

    def test_onboarding_test_shuts_down_when_cancelled_after_start(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        controller.start.side_effect = lambda: (
            tray_application.cancel_onboarding_download(),
            True,
        )[1]
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        tray_application.onboarding_test_runner.thread_pool = ImmediateTaskPool()
        tray_application.run_onboarding_test(AppSettings())

        controller.shutdown.assert_called_once_with()
        controller.test_current_dialog.assert_not_called()
        self.assertEqual(results, [(False, "OCR-to-speech test cancelled.")])
        tray_application.shutdown()

    def test_onboarding_test_displays_the_controller_startup_error(self):
        controller = Mock()
        controller.start.return_value = False
        controller_factory = Mock(return_value=controller)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )
        report_error = controller_factory.call_args.kwargs["error_handler"]
        controller.start.side_effect = lambda: (
            report_error(RuntimeError("invalid Pioneer reference")),
            False,
        )[1]
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        tray_application.onboarding_test_runner.thread_pool = ImmediateTaskPool()
        tray_application.run_onboarding_test(AppSettings())

        self.assertFalse(results[0][0])
        self.assertIn("invalid Pioneer reference", results[0][1])
        tray_application.shutdown()

    def test_onboarding_worker_errors_report_failure_and_allow_retry(self):
        controller = Mock()
        controller.start.return_value = True
        controller.test_current_dialog.return_value = ("Narrator", "Ready.")
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )
        try:
            tray.onboarding_test_runner.thread_pool = ImmediateTaskPool()
            for operation in (
                controller.apply_settings,
                controller.prepare_startup,
                controller.start,
            ):
                with self.subTest(operation=operation):
                    operation.side_effect = RuntimeError("setup failure")
                    tray.run_onboarding_test(AppSettings())
                    self.assertFalse(tray.onboarding_test_runner.active)
                    self.assertFalse(results[-1][0])
                    self.assertIn("setup failure", results[-1][1])
                    operation.side_effect = None
                    tray.run_onboarding_test(AppSettings())
                    self.assertTrue(results[-1][0])
        finally:
            tray.shutdown()

    def test_onboarding_test_waits_for_settings_apply_then_settles_lifecycle(self):
        original = AppSettings()
        candidate = original.updated(output_volume_percent=42)
        entered = Event()
        release = Event()
        controller = Mock(settings=original, is_ready=True, is_live_running=False)

        def blocked_apply(settings, **_options):
            entered.set()
            release.wait(2)
            controller.settings = settings
            return True

        controller.apply_settings.side_effect = blocked_apply
        tray = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        tray.set_ready(True)
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.settings.return_value = candidate
        results = []
        tray.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )
        try:
            with (
                patch("vntts.app.SettingsDialog", return_value=dialog),
                patch.object(
                    tray,
                    "_save_settings_candidate",
                    return_value=Path("settings.json"),
                ),
            ):
                tray.open_settings()
                self.wait_until(entered.is_set)
                tray.run_onboarding_test(candidate)

                self.assertTrue(tray.configuration_runner.active)
                self.assertFalse(tray.onboarding_test_runner.active)
                self.assertEqual(
                    results,
                    [
                        (
                            False,
                            "Reading controls are updating. "
                            "Try the test again when ready.",
                        )
                    ],
                )
                controller.request_shutdown.assert_not_called()

                release.set()
                self.wait_until(lambda: not tray.configuration_runner.active)

            controller.apply_settings.side_effect = lambda settings: (
                setattr(controller, "settings", settings) or True
            )
            controller.start.return_value = True
            controller.test_current_dialog.return_value = ("Narrator", "Ready.")
            tray.onboarding_test_runner.thread_pool = ImmediateTaskPool()
            tray.run_onboarding_test(candidate)

            self.assertTrue(results[-1][0])
            self.assertFalse(tray._controller_busy)
            self.assertFalse(tray.onboarding_test_runner.active)
        finally:
            release.set()
            tray.shutdown()

    def test_shutdown_discards_late_onboarding_test_completion(self):
        entered = Event()
        release = Event()
        completed = Event()
        controller = Mock(is_ready=True)

        def blocked_apply(_settings):
            entered.set()
            release.wait(2)
            completed.set()
            return True

        controller.apply_settings.side_effect = blocked_apply
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )
        try:
            tray.run_onboarding_test(AppSettings())
            self.wait_until(entered.is_set)

            tray.shutdown()
            self.assertFalse(tray.onboarding_test_runner.active)
            release.set()
            self.wait_until(completed.is_set)
            self.application.processEvents()

            self.assertEqual(results, [])
            self.assertTrue(tray._controller_busy)
        finally:
            release.set()

    def test_pocket_onboarding_cancellation_stops_startup_and_reports_cancelled(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        controller.start.side_effect = lambda: (
            tray_application.cancel_onboarding_download(),
            False,
        )[1]
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        tray_application.onboarding_test_runner.thread_pool = ImmediateTaskPool()
        tray_application.run_onboarding_test(AppSettings(speech_backend="pocket-tts"))

        controller.shutdown.assert_called_once_with()
        controller.test_current_dialog.assert_not_called()
        self.assertEqual(results, [(False, "OCR-to-speech test cancelled.")])
        tray_application.shutdown()

    def test_onboarding_cancel_only_signals_the_background_owner(self):
        controller = Mock()
        controller.shutdown.side_effect = AssertionError(
            "Qt cancellation must not shut the controller down"
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        heartbeat = []

        QTimer.singleShot(0, lambda: heartbeat.append(True))
        tray_application.cancel_onboarding_download()
        self.application.processEvents()

        self.assertEqual(heartbeat, [True])
        controller.shutdown.assert_not_called()
        controller.shutdown.side_effect = None
        tray_application.shutdown()

    def test_package_self_test_does_not_start_qt_application(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_package_self_test",
                return_value=CLIReportResult(True, Path("report.json")),
            ) as self_test,
            patch("vntts.app.QApplication") as application,
        ):
            result = main(
                [
                    "--package-self-test",
                    "--package-self-test-report",
                    "custom-report.json",
                ]
            )

        self.assertEqual(result, 0)
        self_test.assert_called_once_with("custom-report.json")
        application.assert_not_called()

    def test_release_smoke_test_does_not_start_qt_application(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_release_smoke_test",
                return_value=CLIReportResult(True, Path("report.json")),
            ) as smoke_test,
            patch("vntts.app.QApplication") as application,
        ):
            result = main(
                [
                    "--release-smoke-test-image",
                    "dialog.png",
                    "--release-smoke-test-report",
                    "custom-report.json",
                    "--release-smoke-test-expected-speaker",
                    "Marcus",
                ]
            )

        self.assertEqual(result, 0)
        smoke_test.assert_called_once_with(
            image_path="dialog.png",
            window_title=None,
            report_path="custom-report.json",
            model_name="tts_models/en/vctk/vits",
            expected_speaker="Marcus",
            auto_advance_expected_text=None,
        )
        application.assert_not_called()

    def test_release_smoke_test_passes_auto_advance_acknowledgement_text(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_release_smoke_test",
                return_value=CLIReportResult(True, Path("report.json")),
            ) as smoke_test,
            patch("vntts.app.QApplication") as application,
        ):
            result = main(
                [
                    "--release-smoke-test-window-title",
                    "VNTTS fixture",
                    "--release-smoke-test-auto-advance-expected-text",
                    "Auto advance acknowledged.",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            smoke_test.call_args.kwargs["auto_advance_expected_text"],
            "Auto advance acknowledged.",
        )
        application.assert_not_called()

    def test_incomplete_release_smoke_options_do_not_start_qt(self):
        for option in (
            "--release-smoke-test-auto-advance-expected-text",
            "--release-smoke-test-expected-speaker",
            "--release-smoke-test-model",
        ):
            with self.subTest(option=option), TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                with (
                    patch("vntts.app.configure_bundled_dependencies"),
                    patch("vntts.app.QApplication") as application,
                ):
                    result = main(
                        [
                            option,
                            "value",
                            "--release-smoke-test-report",
                            str(report_path),
                        ]
                    )
                self.assertEqual(result, 1)
                application.assert_not_called()
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertFalse(report["success"])
                self.assertIn("exactly one", report["checks"][0]["message"])


if __name__ == "__main__":
    unittest.main()
