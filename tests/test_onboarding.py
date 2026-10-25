import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QFont, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.story_fixtures import write_synthetic_game_pack  # noqa: E402
from vntts.calibration import DialogRegionOverlay  # noqa: E402
from vntts.diagnostics import DiagnosticSnapshot  # noqa: E402
from vntts.diagnostics_ui import DiagnosticsDialog  # noqa: E402
from vntts.game_pack import GamePackError, apply_game_pack  # noqa: E402
from vntts.moss_cpp_installation import MossCppInstallRequired  # noqa: E402
from vntts.ocr import DialogRegion  # noqa: E402
from vntts.onboarding import DiagnosticResult, OnboardingDiagnostics  # noqa: E402
from vntts.onboarding_ui import OnboardingWizard  # noqa: E402
from vntts.services.tts_engine import TTSSynthesisError  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.ui_text import plain_label_text  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


def granted_permissions():
    return {"screen_capture": True, "accessibility": True}


class OnboardingDiagnosticsTest(unittest.TestCase):
    def test_hotkey_readiness_reports_runtime_platform_availability(self):
        diagnostics = OnboardingDiagnostics()
        for platform, status in (("darwin", "warning"), ("win32", "ok")):
            with (
                self.subTest(platform=platform),
                patch("vntts.onboarding.sys.platform", platform),
            ):
                result = diagnostics._check_hotkeys(AppSettings())
                self.assertEqual(result.status, status)
                self.assertTrue(result.passed)
                if platform == "darwin":
                    self.assertIn("Global hotkeys are unavailable", result.message)
                    self.assertIn("control window or compact controls", result.message)
                    self.assertIsNone(result.remediation)

    def test_failed_readiness_probe_logs_the_original_exception_chain(self):
        cause = OSError("device disconnected")
        failure = RuntimeError("audio probe failed")
        failure.__cause__ = cause
        diagnostics = OnboardingDiagnostics(audio_probe=Mock(side_effect=failure))
        with self.assertLogs("vntts.onboarding", level="ERROR") as logged:
            result = diagnostics._check_audio()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.message, "audio probe failed")
        self.assertIs(logged.records[0].exc_info[1], failure)
        self.assertIn("device disconnected", logged.output[0])
        with self.assertLogs("vntts.onboarding", level="ERROR") as logged:
            result = diagnostics._check_capture_source(
                AppSettings(capture_mode="window")
            )
        self.assertEqual(result.status, "error")
        self.assertIn("no game window selected", logged.output[0])

    def test_missing_audio_runtime_has_installation_remediation_and_can_recover(self):
        from vntts.rust_audio import AudioRuntimeUnavailable

        probe = Mock(side_effect=AudioRuntimeUnavailable("Run cargo build"))
        diagnostics = OnboardingDiagnostics(audio_probe=probe)
        result = diagnostics._check_audio()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.remediation, "external-audio-runtime")
        self.assertIn("cargo build", result.message)
        probe.side_effect = None
        probe.return_value = {"name": "Speakers"}
        self.assertEqual(diagnostics._check_audio().status, "ok")

    def test_cancelled_setup_skips_subsequent_preparation_and_probes(self):
        for token_kind in ("event", "callable"):
            for phase in ("before", "prepared", "progress"):
                with self.subTest(token_kind=token_kind, phase=phase):
                    cancelled = Event()
                    token = cancelled if token_kind == "event" else cancelled.is_set
                    if phase == "before":
                        cancelled.set()
                    diagnostics = OnboardingDiagnostics()
                    progress = Mock(
                        side_effect=lambda _message: (
                            cancelled.set() if phase == "progress" else None
                        )
                    )
                    with (
                        patch(
                            "vntts.runtime_installation.ensure_speech_runtime",
                            side_effect=lambda *_args, **_kwargs: (
                                cancelled.set() if phase == "prepared" else None
                            ),
                        ) as prepare,
                        patch.object(diagnostics, "run") as run,
                        self.assertRaisesRegex(TTSSynthesisError, "cancelled"),
                    ):
                        diagnostics.prepare_and_run(
                            AppSettings(speech_backend="pocket-tts"),
                            cancellation=token,
                            progress=progress,
                        )
                    self.assertEqual(prepare.call_count, 0 if phase == "before" else 1)
                    run.assert_not_called()

    def test_qwen_setup_and_diagnostics_select_the_same_runtime(self):
        settings = AppSettings(speech_backend="qwen-tts")
        diagnostics = OnboardingDiagnostics()
        progress = Mock()
        paths = (Path("/runtime"), Path("/runtime/python"), Path("/site"))
        for platform, expected in (("darwin", "moss-tts"), ("win32", "qwen-tts")):
            with (
                self.subTest(platform=platform),
                patch("vntts.onboarding.sys.platform", platform),
                patch("vntts.runtime_installation.ensure_speech_runtime") as prepare,
                patch.object(diagnostics, "run", return_value=()),
                patch(
                    "vntts.onboarding.packaged_speech_backend_available",
                    return_value=True,
                ),
                patch(
                    "vntts.onboarding.resolve_speech_runtime_paths",
                    return_value=paths,
                ) as resolve,
            ):
                diagnostics.prepare_and_run(
                    settings, cancellation=None, progress=progress
                )
                self.assertEqual(diagnostics._check_model(settings).status, "ok")
                self.assertEqual(prepare.call_args.args, (expected,))
                self.assertEqual(resolve.call_args.args, (expected,))

    def test_ready_environment_passes_with_valid_voice_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "model"
            model_path.mkdir()
            reference = root / "marcus.wav"
            reference.touch()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Marcus",
                                "speaker": "reverse-1999-marcus",
                                "reference": reference.name,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            diagnostics = OnboardingDiagnostics(
                tesseract_probe=lambda: "5.5.0",
                audio_probe=lambda: "Speakers",
                model_path_resolver=lambda _model: model_path,
                permission_status_provider=granted_permissions,
            )
            settings = AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                speech_backend="coqui-xtts",
                tts_model="xtts_v2",
                voice_manifest=str(manifest),
            )

            with patch("vntts.onboarding.sys.platform", "win32"):
                results = diagnostics.run(settings)

        self.assertTrue(all(result.passed for result in results))
        self.assertTrue(all(result.status == "ok" for result in results))

    def test_missing_external_components_are_actionable_errors(self):
        def fail_tesseract():
            raise RuntimeError("Tesseract executable was not found")

        def fail_audio():
            raise RuntimeError("No output device")

        diagnostics = OnboardingDiagnostics(
            tesseract_probe=fail_tesseract,
            audio_probe=fail_audio,
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=granted_permissions,
        )

        results = diagnostics.run(
            AppSettings(speech_backend="coqui-xtts", tts_model="xtts_v2")
        )
        errors = {result.name: result for result in results if not result.passed}

        self.assertIn("Tesseract executable", errors["Tesseract OCR"].message)
        self.assertIn("No output device", errors["Audio output"].message)
        self.assertEqual(errors["Tesseract OCR"].remediation, "external-ocr")
        self.assertEqual(errors["Audio output"].remediation, "external-audio")
        model_result = next(
            result for result in results if result.name == "Speech model"
        )
        self.assertEqual(model_result.status, "warning")

    def test_missing_voice_reference_is_an_error(self):
        with TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Marcus",
                                "speaker": "marcus",
                                "reference": "missing.wav",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            diagnostics = OnboardingDiagnostics(
                tesseract_probe=lambda: "5.5.0",
                audio_probe=lambda: "Speakers",
                model_path_resolver=lambda _model: Path(temporary_directory),
                permission_status_provider=granted_permissions,
            )

            results = diagnostics.run(
                AppSettings(tts_model="xtts_v2", voice_manifest=str(manifest))
            )

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "error")
        self.assertIn("missing.wav", voice_result.message)
        self.assertEqual(voice_result.remediation, "voices")

    def test_xtts_without_voice_pack_requires_narrator(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=granted_permissions,
        )

        results = diagnostics.run(
            AppSettings(speech_backend="coqui-xtts", tts_model="xtts_v2")
        )

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "error")
        self.assertIn("narrator speaker", voice_result.message)
        self.assertEqual(voice_result.remediation, "settings")

    def test_pocket_ignores_stale_xtts_model_when_using_default_narrator(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            permission_status_provider=granted_permissions,
        )

        results = diagnostics.run(
            AppSettings(speech_backend="pocket-tts", tts_model="xtts_v2")
        )

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "ok")
        self.assertIn("built-in Alba", voice_result.message)

    def test_missing_macos_permissions_block_setup_with_actionable_guidance(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=lambda: {
                "screen_capture": False,
                "accessibility": False,
            },
        )

        results = diagnostics.run(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                tts_model="xtts_v2",
                narrator_speaker="Narrator",
                auto_advance_enabled=True,
                story_index="story-index.jsonl",
                live_sequence_plan="live-sequence.json",
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "error")
        self.assertIn("Screen Recording", permission.message)
        self.assertIn("Accessibility", permission.message)
        self.assertIn("System Settings", permission.message)
        self.assertEqual(permission.remediation, "permissions")

    def test_accessibility_is_optional_without_auto_advance(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=lambda: {
                "screen_capture": True,
                "accessibility": False,
            },
        )

        results = diagnostics.run(
            AppSettings(
                game_window_title="Reverse: 1999",
                tts_model="xtts_v2",
                narrator_speaker="Narrator",
                auto_advance_enabled=False,
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "ok")
        self.assertNotIn("Accessibility", permission.message)

    def test_accessibility_is_optional_for_dormant_default_sequence(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=lambda: {
                "screen_capture": True,
                "accessibility": False,
            },
        )

        results = diagnostics.run(
            AppSettings(
                game_window_title="Reverse: 1999",
                narrator_speaker="Narrator",
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "ok")
        self.assertNotIn("Accessibility", permission.message)


class OnboardingWizardTest(unittest.TestCase):
    def test_manual_managed_content_paths_are_rejected_without_blocking_existing_paths(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            managed, *_ = write_synthetic_game_pack(
                root / "pregeneration" / "jobs" / "job"
            )
            external, *_ = write_synthetic_game_pack(root / "exported")
            settings = AppSettings(game_window_title="Game")
            with patch("vntts.settings.get_local_data_directory", return_value=root):
                wizard = OnboardingWizard(settings)
                page = wizard.configuration_page
                for name in ("game_pack", "voice_manifest"):
                    getattr(page, name).setText(str(managed))
                self.assertFalse(page.validatePage())
                errors = page.validation_errors()
                for name in ("game_pack", "voice_manifest"):
                    self.assertTrue(
                        any(
                            widget is getattr(page, name)
                            and "managed in Stories" in message
                            for widget, message in errors
                        ),
                        name,
                    )
                    getattr(page, name).setText(str(external))
                self.assertTrue(page.validatePage())
                wizard.deleteLater()

                wizard = OnboardingWizard(apply_game_pack(settings, str(managed)))
                self.assertTrue(wizard.configuration_page.validatePage())
                wizard.deleteLater()

    def test_macos_hotkey_warning_explains_buttons_without_installation_action(self):
        with patch("vntts.onboarding.sys.platform", "darwin"):
            result = OnboardingDiagnostics()._check_hotkeys(AppSettings())
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        page = wizard.diagnostics_page

        page._checks_finished((result,), None)

        self.assertTrue(page.isComplete())
        self.assertIn("[WARNING] Hotkeys", page.results.item(0).text())
        self.assertEqual(page.remediation_reason.text(), result.message)
        self.assertTrue(page.remediation_button.isHidden())
        wizard.deleteLater()

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_incomplete_onboarding_preserves_saved_auto_advance_opt_out(self):
        for reading_setup in (False, True):
            with self.subTest(reading_setup=reading_setup):
                settings = AppSettings(
                    onboarding_completed=False,
                    auto_advance_enabled=False,
                    game_window_title="Game",
                )
                wizard = OnboardingWizard(settings, reading_setup=reading_setup)
                try:
                    self.assertFalse(wizard.configuration_page.auto_advance.isChecked())
                    self.assertFalse(
                        wizard.configuration_page.settings().auto_advance_enabled
                    )
                    wizard.reject()
                    self.assertFalse(settings.auto_advance_enabled)
                finally:
                    wizard.deleteLater()

    def test_contextual_reading_setup_keeps_selected_voices_and_discovers_windows(self):
        settings = AppSettings(
            tts_model=None,
            narrator_speaker=None,
            tts_profile="default",
        )
        wizard = OnboardingWizard(
            settings,
            reading_setup=True,
            auto_discover_windows=True,
            window_loader=lambda: (SimpleNamespace(title="Reverse: 1999"),),
        )
        wizard.show()
        self.application.processEvents()
        page = wizard.configuration_page

        self.assertEqual(wizard.windowTitle(), "Set up Reading")
        self.assertEqual(wizard.stack.currentWidget(), page)
        self.assertEqual(len(wizard.pages), 4)
        self.assertTrue(page.game_window.isVisibleTo(wizard))
        self.assertFalse(page.speech_backend.isVisibleTo(wizard))
        self.assertFalse(page.tts_model.isVisibleTo(wizard))
        self.assertFalse(page.advanced_toggle.isVisibleTo(wizard))
        self.assertIn("Alba", page.speech_summary.text())
        page.game_window.setCurrentText("Reverse: 1999")
        self.assertTrue(page.validatePage())
        self.assertEqual(
            wizard.draft_settings,
            settings.updated(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                auto_advance_enabled=True,
            ),
        )
        font = QFont(wizard.font())
        font.setPointSizeF(max(12, font.pointSizeF()) * 2)
        wizard.setFont(font)
        wizard.resize(520, 420)
        wizard.show_page(3)
        wizard.test_page.set_result(True, "Read the visible dialogue.")
        self.application.processEvents()
        self.assertGreater(
            wizard.stack.currentWidget().verticalScrollBar().maximum(), 0
        )
        self.assertTrue(wizard.finish_button.isVisibleTo(wizard))
        self.assertIn("Save setup to return to Reading", wizard.test_page.status.text())
        wizard.show_page(0)
        requested = []
        wizard.voices_requested.connect(lambda: requested.append(True))
        with patch("vntts.onboarding_ui.GameNarratorDialog") as picker:
            page.choose_narrator_button.click()
        picker.assert_not_called()
        self.assertEqual(requested, [True])
        self.assertFalse(wizard.isVisible())
        self.assertFalse(settings.onboarding_completed)
        wizard.deleteLater()

    def test_contextual_reading_requests_license_and_routes_readiness_remedies(self):
        wizard = OnboardingWizard(
            AppSettings(speech_backend="coqui-xtts", game_window_title="Game"),
            reading_setup=True,
        )
        wizard.show()
        self.application.processEvents()
        page = wizard.configuration_page
        self.assertTrue(page.terms.isVisibleTo(wizard))
        self.assertTrue(page.license_label.isVisibleTo(wizard))
        self.assertFalse(page.validatePage())
        page.terms.setChecked(True)
        self.assertTrue(page.validatePage())
        self.assertTrue(wizard.draft_settings.xtts_terms_accepted)

        requested = []
        wizard.voices_requested.connect(lambda: requested.append("voices"))
        wizard.settings_requested.connect(lambda: requested.append("settings"))
        diagnostics = wizard.diagnostics_page
        for name, target, label, icon in (
            ("Capture source", None, "Choose game window", "scan"),
            ("Hotkeys", "settings", "Open Settings", "settings"),
            ("Character voices", "voices", "Open Voices", "audio-lines"),
        ):
            diagnostics._checks_finished(
                (DiagnosticResult(name, "error", "Needs attention", "settings"),),
                None,
            )
            self.assertEqual(diagnostics.remediation_button.text(), label)
            self.assertEqual(
                diagnostics.remediation_button.property("vnttsActionIcon"), icon
            )
            diagnostics.remediation_button.click()
            if target:
                self.assertEqual(requested[-1], target)
            else:
                self.assertIs(wizard.stack.currentWidget(), page)
                self.assertFalse(page.advanced_content.isVisibleTo(wizard))
        wizard.deleteLater()

    def test_reading_setup_keeps_capture_draft_after_visiting_voices(self):
        wizard = OnboardingWizard(
            AppSettings(game_window_title="Original"), reading_setup=True
        )
        wizard.show()
        page = wizard.configuration_page
        page.game_window.setCurrentText("Manually chosen")
        wizard.request_voices()
        self.assertFalse(wizard.isVisible())

        wizard.resume_after_editor(
            AppSettings(game_window_title="Original", speech_backend="pocket-tts")
        )

        self.assertTrue(wizard.isVisible())
        self.assertEqual(page.game_window.currentText(), "Manually chosen")
        self.assertEqual(wizard.draft_settings.game_window_title, "Manually chosen")
        self.assertEqual(wizard.draft_settings.speech_backend, "pocket-tts")
        wizard.close()
        wizard.deleteLater()

    def test_saved_settings_override_only_the_capture_fields_they_change(self):
        original = AppSettings(
            capture_mode="window",
            game_window_title="Original",
            auto_advance_enabled=True,
        )
        wizard = OnboardingWizard(original, reading_setup=True)
        page = wizard.configuration_page
        page.game_window.setCurrentText("Unsaved game")

        wizard.resume_after_editor(
            original.updated(
                capture_mode="screen",
                auto_advance_enabled=False,
                ocr_language="jpn",
            )
        )

        self.assertEqual(wizard.draft_settings.capture_mode, "screen")
        self.assertFalse(wizard.draft_settings.auto_advance_enabled)
        self.assertEqual(wizard.draft_settings.game_window_title, "Unsaved game")
        self.assertEqual(wizard.draft_settings.ocr_language, "jpn")
        wizard.close()
        wizard.deleteLater()

    def test_reading_screen_region_hides_window_and_auto_advance(self):
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        wizard.show()
        page = wizard.configuration_page
        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))

        self.assertEqual(page.title(), "Choose capture source")
        self.assertFalse(page.game_window.isVisibleTo(wizard))
        self.assertFalse(page.auto_advance.isVisibleTo(wizard))
        self.assertFalse(page.auto_advance_reason.isVisibleTo(wizard))
        self.assertIn("dialogue area in step 3", page.subTitle())
        self.assertTrue(page.validatePage())
        wizard.close()
        wizard.deleteLater()

    def test_back_navigation_preserves_valid_calibration_and_test(self):
        wizard = OnboardingWizard(
            AppSettings(game_window_title="Game"),
            diagnostics=Mock(),
            reading_setup=True,
        )
        wizard.diagnostics_page.runner.start = Mock()
        wizard.next_page()
        wizard.diagnostics_page._checks_finished(
            (DiagnosticResult("Capture source", "ok", "Game"),), None
        )
        wizard.next_page()
        wizard.previous_page()
        self.assertEqual(wizard.diagnostics_page.runner.start.call_count, 1)
        self.assertEqual(wizard.diagnostics_page.results.count(), 1)
        self.assertTrue(wizard.diagnostics_page.complete)
        wizard.next_page()
        wizard.calibration_page.finish_calibration(DialogRegion(0.1, 0.2, 0.4, 0.16))
        wizard.next_page()
        wizard.test_page.set_result(True, "Spoke one line")
        wizard.previous_page()
        self.assertTrue(wizard.calibration_page.calibrated)
        wizard.next_page()
        self.assertTrue(wizard.test_page.successful)
        self.assertTrue(wizard.finish_button.isEnabled())

        wizard.previous_page()
        wizard.previous_page()
        wizard.previous_page()
        wizard.configuration_page.game_window.setCurrentText("Another game")
        wizard.next_page()
        self.assertFalse(wizard.calibration_page.calibrated)
        self.assertFalse(wizard.test_page.successful)
        wizard.deleteLater()

    def test_calibration_capture_failure_restores_context_and_allows_retry(self):
        wizard = OnboardingWizard(
            AppSettings(capture_mode="screen"), reading_setup=True
        )
        page = wizard.calibration_page
        pool = ManualThreadPool()
        page.capture_runner.thread_pool = pool
        wizard.show()
        with patch(
            "vntts.onboarding_ui.capture_calibration_background",
            side_effect=RuntimeError("Screen Recording permission denied"),
        ):
            page.calibrate()
            page.open_overlay()
            pool.run_next()
            self.application.processEvents()

        self.assertTrue(wizard.isVisible())
        self.assertFalse(page.isComplete())
        self.assertIn("permission denied", page.status.text())
        self.assertIn("try again", page.status.text())
        self.assertTrue(page.button.isEnabled())
        wizard.close()
        wizard.deleteLater()

    def test_setup_narrator_picker_updates_draft_without_file_or_save(self):
        original = AppSettings(speech_backend="moss-tts")
        wizard = OnboardingWizard(original)
        page = wizard.configuration_page
        candidate = original.updated(
            voice_manifest="chosen-voices.json",
            speaker_announcement_mode="all-speakers",
            announce_speaker_changes=False,
            speech_backend="pocket-tts",
            tts_model=None,
            tts_profile="default",
            pocket_gated_model_accepted=True,
        )
        with (
            patch("vntts.onboarding_ui.GameNarratorDialog") as picker,
            patch.object(AppSettings, "save") as save,
        ):
            picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            picker.return_value.result_settings = candidate
            page.choose_narrator_button.click()
            save.assert_not_called()
        self.assertEqual(
            page._base_settings().speaker_announcement_mode, "all-speakers"
        )
        self.assertFalse(page._base_settings().announce_speaker_changes)
        self.assertEqual(page.narrator_reference.text(), "")
        self.assertEqual(page._base_settings().speech_backend, "pocket-tts")
        self.assertIsNone(page._base_settings().tts_model)
        self.assertEqual(page._base_settings().tts_profile, "default")
        self.assertTrue(page._base_settings().pocket_gated_model_accepted)
        self.assertIn("Pocket", page.speech_summary.text())
        self.assertFalse(
            any(
                widget is page.choose_narrator_button
                for widget, _ in page.validation_errors()
            )
        )
        before = page._base_settings()
        with patch("vntts.onboarding_ui.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Rejected
            page.choose_narrator_button.click()
        self.assertEqual(page._base_settings(), before)
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("moss-tts"))
        self.assertEqual(page._base_settings().tts_profile, "stable")
        wizard.deleteLater()

    def test_character_choice_keeps_manual_narrator_reference_in_setup(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "narrator.wav"
            reference.touch()
            original = AppSettings(
                tts_speaker_wav=str(reference),
            )
            candidate = original
            wizard = OnboardingWizard(original)
            with patch("vntts.onboarding_ui.GameNarratorDialog") as picker:
                picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
                picker.return_value.result_settings = candidate
                wizard.configuration_page.choose_narrator_button.click()
            draft = wizard.configuration_page._base_settings()
            self.assertEqual(draft.tts_speaker_wav, str(reference))
            wizard.deleteLater()

    def test_qwen_accepts_manual_narrator_without_transcript(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "narrator.wav"
            reference.touch()
            wizard = OnboardingWizard(
                AppSettings(speech_backend="qwen-tts", tts_speaker_wav=str(reference))
            )
            page = wizard.configuration_page
            self.assertFalse(
                any(
                    widget is page.choose_narrator_button
                    for widget, _ in page.validation_errors()
                )
            )
            wizard.deleteLater()

    def test_new_setup_defaults_to_window_capture_and_pocket_tts(self):
        wizard = OnboardingWizard(AppSettings())

        self.assertEqual(wizard.configuration_page.capture_mode.currentData(), "window")
        self.assertEqual(
            wizard.configuration_page.speech_backend.currentData(), "pocket-tts"
        )
        self.assertIn("xtts", wizard.configuration_page.tts_model.text())
        self.assertEqual(wizard.configuration_page.tts_language.text(), "en")
        self.assertEqual(
            wizard.configuration_page.narrator_speaker.text(),
            "Claribel Dervla",
        )

    def test_macos_setup_explains_control_window_only_hotkeys(self):
        with patch("vntts.onboarding_ui.sys.platform", "darwin"):
            wizard = OnboardingWizard(AppSettings())

        page = wizard.configuration_page
        self.assertFalse(page.macos_hotkey_notice.isHidden())
        self.assertIn("Global hotkeys are unavailable", page.macos_hotkey_notice.text())
        self.assertIn("compact controls", page.macos_hotkey_notice.text())
        self.assertFalse(page.read_hotkey.isEnabled())
        self.assertFalse(page.live_hotkey.isEnabled())
        wizard.deleteLater()

    def test_finish_requires_successful_end_to_end_test(self):
        wizard = OnboardingWizard(AppSettings())

        wizard.accept()
        self.assertFalse(wizard.settings().onboarding_completed)

        wizard.test_page.set_result(True, "Success")
        wizard.accept()

        self.assertTrue(wizard.settings().onboarding_completed)

    def test_running_end_to_end_test_guards_navigation_and_cancels_truthfully(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show_page(len(wizard.pages) - 1)
        cancelled = []
        wizard.cancel_requested.connect(lambda: cancelled.append(True))

        wizard.test_page.run_test()

        self.assertTrue(wizard.test_page.running)
        self.assertEqual(wizard.test_page.cancel_button.text(), "Cancel test")
        self.assertFalse(wizard.back_button.isEnabled())
        self.assertFalse(wizard.cancel_button.isEnabled())
        wizard.reject()
        self.assertEqual(cancelled, [True])
        self.assertTrue(wizard.test_page.running)
        self.assertEqual(wizard.test_page.cancel_button.text(), "Cancelling...")
        self.assertIn("Cancelling", wizard.test_page.status.text())

        wizard.test_page.set_result(False, "OCR-to-speech test cancelled.")
        self.assertFalse(wizard.test_page.running)
        self.assertTrue(wizard.back_button.isEnabled())
        self.assertTrue(wizard.cancel_button.isEnabled())
        wizard.deleteLater()

    def test_completed_test_hides_progress_and_cancel_for_every_outcome(self):
        for successful, message in (
            (True, "Read a line"),
            (False, "No dialogue"),
            (False, "Test cancelled"),
        ):
            with self.subTest(successful=successful, message=message):
                wizard = OnboardingWizard(AppSettings(), reading_setup=True)
                wizard.show_page(len(wizard.pages) - 1)
                page = wizard.test_page
                page.run_test()
                page.set_progress(55, "Speaking")
                page.set_result(successful, message)
                self.assertTrue(page.progress.isHidden())
                self.assertTrue(page.cancel_button.isHidden())
                self.assertTrue(page.button.isEnabled())
                self.assertEqual(wizard.finish_button.isEnabled(), successful)
                final_text = page.status.text()
                page.set_progress(75, "Late progress")
                self.assertEqual(page.status.text(), final_text)
                wizard.deleteLater()

    def test_test_cancellation_keeps_its_message_and_emits_once(self):
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        wizard.show_page(len(wizard.pages) - 1)
        requests = []
        wizard.cancel_requested.connect(lambda: requests.append(True))
        page = wizard.test_page
        page.run_test()
        page.request_cancel()
        page.set_progress(80, "Late download update")
        page.request_cancel()
        self.assertEqual(requests, [True])
        self.assertIn("Cancelling", page.status.text())
        self.assertTrue(page.running)
        page.set_result(False, "Cancelled")
        page.run_test()
        page.set_progress(20, "Retry running")
        self.assertEqual(page.status.text(), "Retry running")
        page.set_result(False, "Done")
        wizard.deleteLater()

    def test_cancelled_test_ignores_a_late_success(self):
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        wizard.show_page(len(wizard.pages) - 1)
        page = wizard.test_page
        page.run_test()
        page.request_cancel()
        page.set_result(True, "A late completed recording")
        self.assertFalse(page.successful)
        self.assertFalse(wizard.finish_button.isEnabled())
        self.assertIn("cancelled", page.status.text())
        wizard.deleteLater()

    def test_new_calibration_invalidates_previous_test_and_reuses_selected_region(self):
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        first = DialogRegion(0.1, 0.2, 0.4, 0.16)
        changed = DialogRegion(0.1, 0.4, 0.4, 0.2)
        wizard.calibration_page.finish_calibration(first)
        wizard.show_page(len(wizard.pages) - 1)
        wizard.test_page.set_result(True, "Spoke from first area")
        wizard.previous_page()
        wizard.calibration_page.finish_calibration(changed)
        self.assertEqual(wizard.calibration_page.capture_region, changed)
        wizard.next_page()
        self.assertFalse(wizard.test_page.successful)
        self.assertFalse(wizard.finish_button.isEnabled())
        self.assertEqual(wizard.test_page.status.text(), "The test has not run.")
        self.assertEqual(wizard.test_page.button.text(), "Read the visible line")
        wizard.test_page.set_result(True, "Spoke from changed area")
        wizard.previous_page()
        wizard.calibration_page.finish_calibration(changed)
        wizard.next_page()
        self.assertTrue(wizard.finish_button.isEnabled())
        wizard.deleteLater()

    def test_long_test_error_remains_reachable_without_covering_navigation(self):
        for reading_setup in (False, True):
            with self.subTest(reading_setup=reading_setup):
                wizard = OnboardingWizard(AppSettings(), reading_setup=reading_setup)
                wizard.show_page(len(wizard.pages) - 1)
                font = QFont(wizard.font())
                font.setPointSize(22)
                wizard.setFont(font)
                wizard.resize(560, 440)
                message = "<b>Missing voice</b> " + "long/path/" * 100 + " END"
                wizard.test_page.set_result(False, message)
                wizard.show()
                self.application.processEvents()
                scroll = wizard.stack.currentWidget()
                self.assertIsInstance(scroll, QScrollArea)
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                self.assertEqual(
                    wizard.test_page.status.textFormat(), Qt.TextFormat.PlainText
                )
                self.assertEqual(wizard.test_page.status.text(), message)
                for button in (
                    wizard.back_button,
                    wizard.cancel_button,
                    wizard.finish_button,
                ):
                    self.assertTrue(button.isVisibleTo(wizard))
                    self.assertTrue(
                        wizard.rect().contains(
                            button.mapTo(wizard, button.rect().bottomRight())
                        )
                    )
                scroll.ensureWidgetVisible(wizard.test_page.button)
                self.application.processEvents()
                self.assertTrue(
                    scroll.viewport()
                    .rect()
                    .contains(
                        wizard.test_page.button.mapTo(
                            scroll.viewport(), wizard.test_page.button.rect().center()
                        )
                    )
                )
                wizard.close()
                wizard.deleteLater()

    def test_setup_messages_keep_literal_text_and_keyboard_copy(self):
        message = "<b>Voice</b> & missing.wav\n" + "long/path/" * 100 + "END"
        for reading_setup in (False, True):
            for page_name in ("diagnostics_page", "test_page"):
                with self.subTest(reading_setup=reading_setup, page=page_name):
                    wizard = OnboardingWizard(
                        AppSettings(), reading_setup=reading_setup
                    )
                    wizard.diagnostics_page.runner.start = Mock()
                    page = getattr(wizard, page_name)
                    wizard.show_page(wizard.pages.index(page))
                    page.status.setText(message)
                    wizard.show()
                    label = page.status
                    label.setFocus()
                    QTest.keySequence(
                        label, QKeySequence(QKeySequence.StandardKey.SelectAll)
                    )
                    QTest.keySequence(
                        label, QKeySequence(QKeySequence.StandardKey.Copy)
                    )
                    self.assertEqual(self.application.clipboard().text(), message)
                    for other in (
                        wizard.diagnostics_page.status,
                        wizard.diagnostics_page.remediation_reason,
                        wizard.calibration_page.status,
                        wizard.test_page.status,
                        wizard.configuration_page.validation_summary,
                    ):
                        self.assertEqual(other.textFormat(), Qt.TextFormat.PlainText)
                    wizard.close()
                    wizard.deleteLater()

    def test_incomplete_game_selection_disables_next_and_highlights_refresh(self):
        wizard = OnboardingWizard(
            AppSettings(game_window_title="Game"), reading_setup=True
        )
        wizard.show()
        self.assertTrue(wizard.next_button.isEnabled())
        wizard.configuration_page.game_window.setCurrentText("")
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertFalse(wizard.next_button.isDefault())
        self.assertTrue(wizard.configuration_page.refresh_button.isDefault())
        font = QFont(wizard.font())
        font.setPointSize(22)
        wizard.setFont(font)
        self.assertEqual(
            wizard.configuration_page.validation_summary.font().pointSize(), 22
        )
        self.assertTrue(
            wizard.configuration_page.configuration_scroll.isAncestorOf(
                wizard.configuration_page.validation_summary
            )
        )
        wizard.configuration_page.game_window.setCurrentText("Another game")
        self.assertTrue(wizard.next_button.isEnabled())
        self.assertTrue(wizard.next_button.isDefault())
        wizard.close()
        wizard.deleteLater()

    def test_default_action_follows_current_step_without_bypassing_test(self):
        wizard = OnboardingWizard(AppSettings(), reading_setup=True)
        wizard.diagnostics_page.runner.start = Mock()
        wizard.show_page(wizard.pages.index(wizard.diagnostics_page))
        wizard.diagnostics_page._checks_finished(
            (DiagnosticResult("Capture", "ok", "Ready"),), None
        )
        wizard.show()
        self.assertTrue(wizard.next_button.isDefault())
        QTest.keyClick(wizard, Qt.Key.Key_Return)
        self.assertIs(wizard.pages[wizard.current_page_index], wizard.calibration_page)
        self.assertTrue(wizard.calibration_page.button.isDefault())
        wizard.calibration_page.finish_calibration(DialogRegion(0.1, 0.2, 0.4, 0.16))
        self.assertTrue(wizard.next_button.isDefault())
        wizard.next_page()
        requests = []
        wizard.test_requested.connect(requests.append)
        self.assertTrue(wizard.test_page.button.isDefault())
        QTest.keyClick(wizard, Qt.Key.Key_Return)
        self.assertEqual(len(requests), 1)
        self.assertFalse(wizard.test_page.button.isDefault())
        QTest.keyClick(wizard, Qt.Key.Key_Return)
        self.assertEqual(len(requests), 1)
        wizard.test_page.set_result(False, "Try again")
        self.assertTrue(wizard.test_page.button.isDefault())
        wizard.test_page.run_test()
        wizard.test_page.set_result(True, "Read one line")
        self.assertTrue(wizard.finish_button.isDefault())
        self.assertFalse(wizard.test_page.button.isDefault())
        QTest.keyClick(wizard, Qt.Key.Key_Return)
        self.assertEqual(wizard.result(), QDialog.DialogCode.Accepted)
        wizard.deleteLater()

    def test_configuration_gives_text_fields_room_to_expand(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page

        self.assertTrue(page.configuration_scroll.widgetResizable())
        self.assertEqual(wizard.minimumWidth(), 520)
        self.assertEqual(wizard.minimumHeight(), 420)
        self.assertEqual(page.window_layout.stretch(0), 1)
        self.assertEqual(page.manifest_layout.stretch(0), 1)
        self.assertGreaterEqual(page.refresh_button.minimumWidth(), 120)
        self.assertGreaterEqual(page.browse_manifest_button.minimumWidth(), 120)
        self.assertEqual(
            page.manage_assets_button.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
        wizard.deleteLater()

    def test_step_progress_and_navigation_are_keyboard_accessible(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show()
        wizard.next_button.setFocus()

        self.assertEqual(wizard.step_label.text(), "Step 1 of 5")
        self.assertTrue(wizard.step_label.accessibleName())
        QTest.keyClick(wizard.next_button, Qt.Key.Key_Return)

        self.assertEqual(wizard.current_page_index, 1)
        self.assertEqual(wizard.step_label.text(), "Step 2 of 5")
        wizard.deleteLater()

    def test_configuration_shortcut_errors_identify_each_empty_recorder(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))
        page.live_hotkey.clear()

        self.assertEqual(
            page.validation_errors(),
            (
                (
                    page.live_hotkey,
                    "Keyboard shortcuts: Live reading: press a shortcut.",
                ),
            ),
        )
        page.read_hotkey.clear()
        self.assertEqual(
            [widget for widget, _ in page.validation_errors()],
            [page.read_hotkey, page.live_hotkey],
        )
        wizard.deleteLater()

    def test_incomplete_shortcuts_do_not_block_configuration_previews(self):
        original = AppSettings()
        wizard = OnboardingWizard(original)
        page = wizard.configuration_page
        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))
        page.live_hotkey.clear()
        self.assertEqual(
            [widget for widget, _ in page.update_validation_summary()],
            [page.live_hotkey],
        )
        with patch("vntts.onboarding_ui.GameNarratorDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Rejected
            page.choose_narrator()
            self.assertEqual(picker.call_args.args[0].live_hotkey, original.live_hotkey)
        with patch("vntts.onboarding_ui.AssetManagerDialog") as assets:
            assets.return_value.exec.return_value = QDialog.DialogCode.Rejected
            page.manage_assets()
            self.assertEqual(assets.call_args.args[0].live_hotkey, original.live_hotkey)
        with self.assertRaisesRegex(ValueError, "press a shortcut"):
            page.settings()
        with TemporaryDirectory() as directory:
            page.game_pack.setText(str(Path(directory) / "missing-pack.json"))
            self.assertIn(
                page.game_pack, [widget for widget, _ in page.validation_errors()]
            )
        page.game_pack.clear()
        page.read_hotkey.set_hotkey("<ctrl>+j")
        page.live_hotkey.set_hotkey("<ctrl>+k")
        self.assertEqual(page.settings().read_hotkey, "<ctrl>+j")
        self.assertEqual(page.settings().live_hotkey, "<ctrl>+k")
        wizard.deleteLater()

    def test_configuration_validation_lists_all_errors_and_focuses_first(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        wizard.show_page(1)
        wizard.show()
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("coqui-xtts"))
        page.tts_model.clear()
        page.ocr_language.clear()
        page.tts_language.clear()
        page.terms.setChecked(False)

        self.assertFalse(page.validatePage())
        self.application.processEvents()

        self.assertIn("Game window", page.validation_summary.text())
        self.assertIn("Speech model", page.validation_summary.text())
        self.assertIn("OCR language", page.validation_summary.text())
        self.assertIn("TTS language", page.validation_summary.text())
        self.assertIn("XTTS license", page.validation_summary.text())
        self.assertTrue(page.game_window.hasFocus())

        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("pocket-tts"))
        page.ocr_language.setText("eng")
        self.assertEqual(page.validation_summary.text(), "Configuration is ready.")
        self.assertTrue(page.validatePage())
        wizard.deleteLater()

    def test_configuration_defaults_pocket_to_public_presets_and_persists_opt_in(self):
        wizard = OnboardingWizard(AppSettings(speech_backend="pocket-tts"))
        page = wizard.configuration_page
        wizard.show_page(1)
        wizard.show()
        page.advanced_toggle.click()

        self.assertTrue(page.pocket_gated_model.isVisibleTo(wizard))
        self.assertFalse(page.pocket_gated_model.isChecked())
        self.assertFalse(page.terms.isVisibleTo(wizard))
        self.assertEqual(page.manage_assets_button.text(), "Import character voices...")
        self.assertEqual(
            page.manage_assets_button.property("vnttsActionIcon"), "folder-open"
        )
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("coqui-xtts"))
        self.assertEqual(
            page.manage_assets_button.text(),
            "Download model or import voices...",
        )
        self.assertEqual(
            page.manage_assets_button.property("vnttsActionIcon"), "download"
        )
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("pocket-tts"))

        page.pocket_gated_model.setChecked(True)

        self.assertTrue(page.settings().pocket_gated_model_accepted)
        wizard.deleteLater()

    def test_configuration_scrolls_at_scaled_fonts(self):
        base_font = QApplication.font()
        base_size = base_font.pointSizeF() if base_font.pointSizeF() > 0 else 12.0
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                font = QFont(base_font)
                font.setPointSizeF(base_size * scale)
                wizard = OnboardingWizard(AppSettings())
                wizard.setFont(font)
                wizard.resize(520, 420)
                wizard.show_page(1)
                wizard.show()
                self.application.processEvents()

                page = wizard.configuration_page
                page._set_advanced_expanded(True)
                self.application.processEvents()
                self.assertTrue(page.validation_summary.isVisibleTo(wizard))
                self.assertTrue(page.configuration_scroll.isVisibleTo(wizard))
                self.assertGreater(
                    page.configuration_scroll.verticalScrollBar().maximum(), 0
                )
                self.assertTrue(wizard.next_button.isVisibleTo(wizard))

                wizard.close()
                wizard.deleteLater()

    def test_recommended_setup_hides_technical_fields_until_requested(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show_page(1)
        wizard.show()
        self.application.processEvents()
        page = wizard.configuration_page

        self.assertTrue(page.game_window.isVisibleTo(wizard))
        self.assertFalse(page.game_pack.isVisibleTo(wizard))
        self.assertTrue(page.advanced_toggle.isVisibleTo(wizard))
        self.assertFalse(page.advanced_content.isVisibleTo(wizard))
        self.assertIn("Select your game and speech engine", page.subTitle())
        self.assertTrue(page.speech_backend.isVisibleTo(wizard))
        self.assertTrue(page.speech_summary.isVisibleTo(wizard))
        self.assertIn("Narrator: Alba", plain_label_text(page.speech_summary))

        page.advanced_toggle.click()

        self.assertTrue(page.advanced_content.isVisibleTo(wizard))
        self.assertTrue(page.game_pack.isVisibleTo(wizard))
        self.assertTrue(page.speech_backend.isVisibleTo(wizard))
        self.assertEqual(page.advanced_toggle.text(), "Hide advanced options")
        wizard.deleteLater()

    def test_recommended_setup_discloses_auto_advance_before_diagnostics(self):
        wizard = OnboardingWizard(AppSettings(auto_advance_key="space"))
        wizard.show_page(1)
        wizard.show()
        self.application.processEvents()
        page = wizard.configuration_page

        self.assertLess(
            wizard.pages.index(page),
            wizard.pages.index(wizard.diagnostics_page),
        )
        self.assertTrue(page.auto_advance.isVisibleTo(wizard))
        self.assertTrue(page.auto_advance.isChecked())
        self.assertTrue(page.auto_advance_notice.isVisibleTo(wizard))
        self.assertIn(
            "automatically sends the Space key", page.auto_advance_notice.text()
        )
        if sys.platform == "darwin":
            self.assertIn("Accessibility permission", page.auto_advance_notice.text())

        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))

        self.assertFalse(page.auto_advance.isEnabled())
        self.assertFalse(page.auto_advance.isChecked())
        self.assertFalse(page.auto_advance_reason.isHidden())
        self.assertIn("selected game window", page.auto_advance_reason.text())
        self.assertFalse(page.settings().auto_advance_enabled)
        wizard.deleteLater()

    def test_window_discovery_requires_selection_and_preserves_cleared_selection(self):
        for reading_setup in (False, True):
            with self.subTest(reading_setup=reading_setup):
                wizard = OnboardingWizard(
                    AppSettings(),
                    reading_setup=reading_setup,
                    window_loader=lambda: (
                        SimpleNamespace(title="Unrelated window"),
                        SimpleNamespace(title="Reverse: 1999"),
                    ),
                    auto_discover_windows=True,
                )
                try:
                    wizard.show_page(0 if reading_setup else 1)
                    page = wizard.configuration_page
                    self.assertEqual(page.game_window.count(), 2)
                    self.assertEqual(page.game_window.currentText(), "")
                    self.assertFalse(page.isComplete())
                    self.assertFalse(wizard.next_button.isEnabled())

                    page.game_window.setCurrentIndex(1)
                    page.refresh_windows()
                    self.assertEqual(page.game_window.currentText(), "Reverse: 1999")
                    self.assertTrue(wizard.next_button.isEnabled())

                    page.game_window.setCurrentText("")
                    page.refresh_windows()
                    self.assertEqual(page.game_window.currentText(), "")
                    self.assertFalse(page.isComplete())
                    self.assertFalse(wizard.next_button.isEnabled())
                finally:
                    wizard.deleteLater()

    def test_window_discovery_runs_on_entry_and_preserves_manual_title(self):
        loaded = []

        def windows():
            loaded.append(True)
            return (
                SimpleNamespace(title="Reverse: 1999"),
                SimpleNamespace(title="Another window"),
            )

        wizard = OnboardingWizard(
            AppSettings(game_window_title="My manual game title"),
            window_loader=windows,
            auto_discover_windows=True,
        )

        wizard.show_page(1)

        page = wizard.configuration_page
        self.assertEqual(loaded, [True])
        self.assertEqual(page.game_window.currentText(), "My manual game title")
        self.assertEqual(page.game_window.count(), 2)
        self.assertIn("Found 2", page.window_help.text())
        wizard.deleteLater()

    def test_game_pack_is_applied_as_one_verified_configuration(self):
        def apply(settings, path=None):
            return settings.updated(
                game_pack=path or settings.game_pack,
                story_index="pack/story-index.jsonl",
                live_sequence_plan="pack/live-sequence.json",
                voice_manifest="pack/voices.json",
                generated_audio_manifest="pack/generated.json",
            )

        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        page.game_window.setCurrentText("Reverse: 1999")
        with patch("vntts.onboarding_ui.apply_game_pack", side_effect=apply):
            page.game_pack.setText("pack/game-pack.json")
            self.assertTrue(page.validatePage())

        self.assertEqual(wizard.draft_settings.game_pack, "pack/game-pack.json")
        self.assertEqual(
            wizard.draft_settings.live_sequence_plan,
            "pack/live-sequence.json",
        )
        self.assertEqual(wizard.draft_settings.voice_manifest, "pack/voices.json")
        wizard.deleteLater()

    def test_invalid_game_pack_stays_on_configuration_with_plain_error(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show_page(1)
        page = wizard.configuration_page
        page.game_window.setCurrentText("Reverse: 1999")
        with patch(
            "vntts.onboarding_ui.apply_game_pack",
            side_effect=GamePackError("checksum changed"),
        ):
            page.game_pack.setText("broken/game-pack.json")
            self.assertFalse(page.validatePage())

        self.assertIn("Game pack: checksum changed", page.validation_summary.text())
        self.assertEqual(wizard.current_page_index, 1)
        wizard.deleteLater()

    def test_diagnostics_page_is_async_cancellable_and_stale_safe(self):

        diagnostics = Mock()
        diagnostics.run.return_value = (DiagnosticResult("Capture", "ok", "Ready"),)
        wizard = OnboardingWizard(AppSettings(), diagnostics=diagnostics)
        pool = ManualThreadPool()
        wizard.diagnostics_page.runner.thread_pool = pool

        wizard.show_page(2)
        self.assertTrue(wizard.diagnostics_page.runner.active)
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertEqual(wizard.diagnostics_page.results.count(), 0)

        wizard.diagnostics_page.cancel_checks()
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertEqual(wizard.diagnostics_page.results.count(), 0)
        self.assertIn("cancelled", wizard.diagnostics_page.status.text().casefold())

        wizard.diagnostics_page.start_checks()
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertTrue(wizard.diagnostics_page.complete)
        self.assertTrue(wizard.next_button.isEnabled())
        self.assertEqual(wizard.diagnostics_page.results.count(), 1)
        wizard.deleteLater()

    def test_diagnostics_selects_first_error_and_opens_advanced_setup(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.diagnostics_page

        page._checks_finished(
            (
                DiagnosticResult("Capture", "ok", "Ready"),
                DiagnosticResult(
                    "Speech runtime",
                    "error",
                    "Pocket runtime is missing",
                    "settings",
                ),
            ),
            None,
        )

        self.assertEqual(page.results.currentRow(), 1)
        self.assertEqual(page.remediation_button.text(), "Open setup options")
        self.assertEqual(
            page.remediation_button.property("vnttsActionIcon"), "settings"
        )
        page.remediation_button.click()
        self.assertEqual(wizard.current_page_index, 1)
        self.assertTrue(wizard.configuration_page.advanced_toggle.isChecked())
        wizard.deleteLater()

    def test_runtime_preparation_has_progress_retry_and_cancellable_stale_results(self):

        diagnostics = OnboardingDiagnostics()
        diagnostics.prepare_and_run = Mock(
            side_effect=[
                RuntimeError("Download interrupted"),
                (DiagnosticResult("Runtime", "ok", "Ready"),),
            ]
        )
        wizard = OnboardingWizard(AppSettings(), diagnostics=diagnostics)
        page = wizard.diagnostics_page
        pool = ManualThreadPool()
        page.runner.thread_pool = pool
        wizard.show_page(2)
        first = page.cancellation
        page.runtime_progress.emit(first, "Downloading locked speech dependencies...")
        self.assertIn("Downloading", page.status.text())
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertFalse(page.retry_button.isEnabled())
        self.assertTrue(page.cancel_button.isEnabled())
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertIn("Download interrupted", page.status.text())
        self.assertTrue(page.retry_button.isEnabled())
        page.retry_button.click()
        self.assertTrue(first.is_set())
        page.runtime_progress.emit(first, "Stale download status")
        self.assertNotIn("Stale", page.status.text())
        second = page.cancellation
        wizard.reject()
        self.assertTrue(second.is_set())
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertFalse(page.complete)
        self.assertIn("cancelled", page.status.text().lower())
        wizard.deleteLater()

    def test_first_moss_install_waits_for_explicit_action(self):

        diagnostics = OnboardingDiagnostics()
        diagnostics.moss_installation_space = Mock(
            return_value=(9_100_000_000, 9_300_000_000, 20_000_000_000)
        )
        diagnostics.prepare_and_run = Mock(
            return_value=(DiagnosticResult("Runtime", "ok", "Ready"),)
        )
        wizard = OnboardingWizard(
            AppSettings(speech_backend="moss-tts"), diagnostics=diagnostics
        )
        page = wizard.diagnostics_page
        pool = ManualThreadPool()
        page.runner.thread_pool = pool

        wizard.show_page(2)

        self.assertEqual(pool.tasks, [])
        self.assertFalse(page.install_moss_button.isHidden())
        self.assertTrue(page.install_moss_button.isEnabled())
        self.assertTrue(page.install_moss_button.isDefault())
        self.assertIn("9.1 GB download", page.status.text())
        self.assertIn("20.0 GB", page.status.text())

        page.install_moss_button.click()
        pool.tasks.pop().run()
        self.application.processEvents()

        self.assertTrue(page.complete)
        self.assertTrue(
            diagnostics.prepare_and_run.call_args.kwargs["allow_moss_download"]
        )
        wizard.deleteLater()

    def test_moss_install_space_can_be_refreshed(self):
        diagnostics = OnboardingDiagnostics()
        diagnostics.moss_installation_space = Mock(
            side_effect=[
                (9_100_000_000, 9_300_000_000, 8_000_000_000),
                (9_100_000_000, 9_300_000_000, 20_000_000_000),
            ]
        )
        wizard = OnboardingWizard(
            AppSettings(speech_backend="moss-tts"), diagnostics=diagnostics
        )
        page = wizard.diagnostics_page

        wizard.show_page(2)
        self.assertFalse(page.install_moss_button.isEnabled())
        self.assertEqual(page.retry_button.text(), "Refresh disk space")

        page.retry_button.click()

        self.assertTrue(page.install_moss_button.isEnabled())
        self.assertIn("20.0 GB", page.status.text())
        wizard.deleteLater()

    def test_corrupt_moss_install_can_be_explicitly_repaired(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.diagnostics_page

        page._checks_finished(
            (), MossCppInstallRequired(4_600_000_000, 4_800_000_000, 9_000_000_000)
        )

        self.assertFalse(page.install_moss_button.isHidden())
        self.assertTrue(page.install_moss_button.isEnabled())
        self.assertIn("4.6 GB download", page.status.text())
        wizard.deleteLater()

    def test_diagnostics_explains_external_dependency_installation(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.diagnostics_page
        page._checks_finished(
            (
                DiagnosticResult(
                    "Tesseract OCR",
                    "error",
                    "Tesseract executable was not found",
                ),
            ),
            None,
        )

        self.assertEqual(page.remediation_button.text(), "Show installation help")
        self.assertEqual(
            page.remediation_button.property("vnttsActionIcon"), "circle-help"
        )
        page.remediation_button.click()

        self.assertIn("Requirements section", page.remediation_reason.text())
        self.assertFalse(page.remediation_button.isEnabled())
        wizard.deleteLater()

    def test_successful_test_has_one_explicit_finish_handoff(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show_page(len(wizard.pages) - 1)

        wizard.test_page.set_result(True, "Success. Recognized Rhiannon: Hello.")

        self.assertEqual(wizard.finish_button.text(), "Finish setup")
        self.assertTrue(wizard.finish_button.isDefault())
        self.assertTrue(wizard.finish_button.isEnabled())
        self.assertIn("Prepare offline audio", wizard.test_page.status.text())
        self.assertIn("Start reading", wizard.test_page.status.text())
        self.assertEqual(wizard.test_page.button.text(), "Run test again")
        wizard.deleteLater()

    def test_first_run_journey_needs_only_game_window_before_guided_test(self):

        diagnostics = Mock()
        diagnostics.run.return_value = (
            DiagnosticResult("Capture", "ok", "Game window is ready"),
            DiagnosticResult("Speech", "ok", "Pocket speech is ready"),
        )
        wizard = OnboardingWizard(
            AppSettings(),
            diagnostics=diagnostics,
            window_loader=lambda: (SimpleNamespace(title="Reverse: 1999"),),
            auto_discover_windows=True,
        )
        pool = ManualThreadPool()
        wizard.diagnostics_page.runner.thread_pool = pool
        test_requests = []
        wizard.test_requested.connect(test_requests.append)

        wizard.next_page()
        self.assertEqual(wizard.current_page_index, 1)
        self.assertEqual(wizard.configuration_page.game_window.currentText(), "")
        wizard.configuration_page.game_window.setCurrentText("Reverse: 1999")
        self.assertEqual(
            wizard.configuration_page.game_window.currentText(),
            "Reverse: 1999",
        )
        self.assertFalse(wizard.configuration_page.advanced_content.isVisible())

        wizard.next_page()
        self.assertEqual(wizard.current_page_index, 2)
        pool.tasks.pop().run()
        self.application.processEvents()
        self.assertTrue(wizard.diagnostics_page.complete)

        wizard.next_page()
        wizard.calibration_page.finish_calibration(DialogRegion(0.1, 0.2, 0.4, 0.16))
        wizard.next_page()
        wizard.test_page.button.click()
        self.assertEqual(len(test_requests), 1)
        wizard.test_page.set_result(True, "Recognized and spoke the visible line.")
        wizard.finish_button.click()

        self.assertTrue(wizard.settings().onboarding_completed)
        self.assertEqual(wizard.settings().game_window_title, "Reverse: 1999")
        self.assertEqual(wizard.settings().speech_backend, "pocket-tts")
        wizard.deleteLater()

    def test_composite_configuration_fields_have_accessible_labels(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        labels = {
            label.text(): label
            for label in page.findChildren(QLabel)
            if label.text() in {"Game window", "Narrator reference", "Voice manifest"}
        }

        self.assertIs(labels["Game window"].buddy(), page.game_window)
        self.assertIs(labels["Narrator reference"].buddy(), page.narrator_reference)
        self.assertIs(labels["Voice manifest"].buddy(), page.voice_manifest)
        for field in (
            page.game_window,
            page.narrator_reference,
            page.voice_manifest,
            page.refresh_button,
            page.narrator_reference_button,
            page.browse_manifest_button,
        ):
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
        wizard.deleteLater()

    def test_calibration_hides_wizard_before_frozen_overlay_capture(self):
        target = Mock()
        target.get_geometry.return_value = WindowGeometry(0, 0, 1000, 600)
        with TemporaryDirectory() as temporary_directory:
            wizard = OnboardingWizard(
                AppSettings(
                    capture_mode="window",
                    game_window_title="Reverse: 1999",
                ),
                capture_target_factory=Mock(return_value=target),
            )
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                platform="darwin",
            )
            pool = ManualThreadPool()
            wizard.calibration_page.capture_runner.thread_pool = pool
            wizard.show()
            other, hidden = QWidget(), QWidget()
            other.show()
            background = Image.new("RGB", (16, 16))

            def show_overlay(_geometry, **_kwargs):
                overlay.show()
                return overlay

            with (
                patch(
                    "vntts.onboarding_ui.show_calibration_overlay",
                    side_effect=show_overlay,
                ) as show,
                patch(
                    "vntts.onboarding_ui.capture_calibration_background",
                    return_value=background,
                ),
            ):
                wizard.calibration_page.calibrate()
                self.assertFalse(wizard.isVisible())
                self.assertFalse(other.isVisible())
                wizard.calibration_page.open_overlay()
                pool.run_next()
                self.application.processEvents()
                self.assertIs(show.call_args.kwargs["background"], background)

            self.assertTrue(overlay.isVisible())
            self.assertFalse(wizard.isVisible())

            overlay.close()
            self.application.processEvents()

            self.assertTrue(wizard.isVisible())
            self.assertTrue(other.isVisible())
            self.assertFalse(hidden.isVisible())
            other.close()
            other.deleteLater()
            hidden.deleteLater()
            wizard.close()
            wizard.deleteLater()

    def test_reading_setup_passes_profile_aware_region_save_to_overlay(self):
        save_region = Mock()
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)
        wizard = OnboardingWizard(
            AppSettings(active_profile_id="active", capture_mode="screen"),
            reading_setup=True,
            save_region=save_region,
            capture_region=region,
        )
        pool = ManualThreadPool()
        wizard.calibration_page.capture_runner.thread_pool = pool
        background = Image.new("RGB", (16, 16))
        with (
            patch("vntts.onboarding_ui.show_calibration_overlay") as show_overlay,
            patch(
                "vntts.onboarding_ui.capture_calibration_background",
                return_value=background,
            ),
        ):
            wizard.calibration_page.calibrate()
            wizard.calibration_page.open_overlay()
            pool.run_next()
            self.application.processEvents()

        show_overlay.assert_called_once_with(
            None, background=background, save_region=save_region, capture_region=region
        )
        save_region.assert_not_called()
        wizard.close()
        wizard.deleteLater()

    def test_calibration_invalid_background_restores_survivors_without_opening_overlay(
        self,
    ):
        wizard = OnboardingWizard(AppSettings(capture_mode="screen"))
        other = QWidget()
        wizard.show()
        other.show()
        page = wizard.calibration_page
        pool = ManualThreadPool()
        page.capture_runner.thread_pool = pool
        try:
            with (
                patch(
                    "vntts.onboarding_ui.capture_calibration_background",
                    return_value=object(),
                ),
                patch("vntts.onboarding_ui.show_calibration_overlay") as overlay,
                patch.object(other, "activateWindow") as activate,
                patch.object(other, "raise_") as raise_window,
            ):
                page.calibrate()
                page.open_overlay()
                pool.run_next()
                self.application.processEvents()
                overlay.assert_not_called()
                self.assertTrue(wizard.isVisible())
                self.assertTrue(other.isVisible())
                self.assertIn("no image returned", page.status.text())
                activate.assert_not_called()
                raise_window.assert_not_called()
        finally:
            for window in (wizard, other):
                window.close()
                window.deleteLater()

    def test_calibration_finish_cancels_duplicate_pending_overlay_launches(self):
        wizard = OnboardingWizard(AppSettings(capture_mode="screen"))
        page = wizard.calibration_page
        try:
            with patch("vntts.onboarding_ui.show_calibration_overlay") as show_overlay:
                page.calibrate()
                self.assertTrue(page.capture_timer.isActive())
                with patch.object(page.capture_timer, "start") as schedule_overlay:
                    page.calibrate()
                    schedule_overlay.assert_not_called()

                wizard.done(QDialog.DialogCode.Rejected)
                self.assertFalse(page.capture_timer.isActive())
                page.open_overlay()

                show_overlay.assert_not_called()
                self.assertIsNone(page.overlay)
        finally:
            wizard.deleteLater()

    def test_calibration_close_discards_pending_capture_and_restores_other_windows(
        self,
    ):
        wizard = OnboardingWizard(AppSettings(capture_mode="screen"))
        other, hidden = QWidget(), QWidget()
        wizard.show()
        other.show()
        page = wizard.calibration_page
        pool = ManualThreadPool()
        page.capture_runner.thread_pool = pool
        try:
            with (
                patch("vntts.onboarding_ui.capture_calibration_background") as capture,
                patch("vntts.onboarding_ui.show_calibration_overlay") as show,
                patch.object(wizard, "activateWindow") as activate,
                patch.object(wizard, "raise_") as raise_window,
            ):
                page.calibrate()
                page.open_overlay()
                self.assertTrue(page.capture_runner.active)
                wizard.done(QDialog.DialogCode.Rejected)
                pool.run_next()
                self.application.processEvents()
                capture.assert_not_called()
                show.assert_not_called()
                activate.assert_not_called()
                raise_window.assert_not_called()
            self.assertFalse(wizard.isVisible())
            self.assertTrue(other.isVisible())
            self.assertFalse(hidden.isVisible())
            self.assertIsNone(page.overlay)
        finally:
            for window in (wizard, other, hidden):
                window.close()
                window.deleteLater()

    def test_calibration_owner_deletion_cancels_acquisition_and_restores_other_windows(
        self,
    ):
        wizard = OnboardingWizard(AppSettings(capture_mode="screen"))
        other = QWidget()
        wizard.show()
        other.show()
        page = wizard.calibration_page
        pool = ManualThreadPool()
        page.capture_runner.thread_pool = pool
        try:
            with (
                patch("vntts.onboarding_ui.capture_calibration_background") as capture,
                patch("vntts.onboarding_ui.show_calibration_overlay") as show,
                patch.object(other, "activateWindow") as activate,
                patch.object(other, "raise_") as raise_window,
            ):
                page.calibrate()
                page.open_overlay()
                cancelled = page._capture_cancelled
                wizard.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertTrue(cancelled.is_set())
                self.assertTrue(other.isVisible())
                pool.run_next()
                self.application.processEvents()
                capture.assert_not_called()
                show.assert_not_called()
                activate.assert_not_called()
                raise_window.assert_not_called()
        finally:
            other.close()
            other.deleteLater()

    def test_calibration_suspends_pending_diagnostics_until_overlay_closes(self):
        wizard = OnboardingWizard(AppSettings(capture_mode="screen"))
        dialog = DiagnosticsDialog(refresh_timeout_ms=60_000)
        wizard.show()
        dialog.show()
        dialog.request_refresh()
        generation = dialog.refresh_generation
        dialog.conceal_for_capture()
        pool = ManualThreadPool()
        page = wizard.calibration_page
        page.capture_runner.thread_pool = pool
        overlay = Mock()
        try:
            with (
                patch(
                    "vntts.onboarding_ui.capture_calibration_background",
                    return_value=Image.new("RGB", (16, 16)),
                ),
                patch(
                    "vntts.onboarding_ui.show_calibration_overlay", return_value=overlay
                ),
                patch.object(dialog, "activateWindow") as activate,
                patch.object(dialog, "raise_") as raise_window,
            ):
                page.calibrate()
                dialog._refresh_timed_out(generation)
                dialog.set_snapshot(DiagnosticSnapshot(None, text="Late snapshot"))
                self.assertFalse(dialog.isVisible())
                self.assertFalse(dialog.refresh_timer.isActive())
                page.open_overlay()
                pool.run_next()
                self.application.processEvents()
                self.assertFalse(dialog.isVisible())
                overlay.closed.connect.call_args.args[0]()
                self.assertTrue(dialog.isVisible())
                activate.assert_not_called()
                raise_window.assert_not_called()
        finally:
            for window in (wizard, dialog):
                window.close()
                window.deleteLater()

    def test_calibration_finish_closes_active_overlay_without_restoring_wizard(self):
        save_region = Mock()
        wizard = OnboardingWizard(
            AppSettings(capture_mode="screen"), save_region=save_region
        )
        page = wizard.calibration_page
        overlay = Mock()
        pool = ManualThreadPool()
        page.capture_runner.thread_pool = pool
        wizard.show()
        try:
            with (
                patch(
                    "vntts.onboarding_ui.show_calibration_overlay", return_value=overlay
                ),
                patch(
                    "vntts.onboarding_ui.capture_calibration_background",
                    return_value=Image.new("RGB", (16, 16)),
                ),
            ):
                page.calibrate()
                page.open_overlay()
                pool.run_next()
                self.application.processEvents()
                close_overlay = overlay.closed.connect.call_args.args[0]
                overlay.close.side_effect = close_overlay

                wizard.done(QDialog.DialogCode.Rejected)

                overlay.close.assert_called_once_with()
                overlay.deleteLater.assert_called_once_with()
                self.assertIsNone(page.overlay)
                self.assertFalse(wizard.isVisible())
                save_region.assert_not_called()
        finally:
            wizard.deleteLater()


if __name__ == "__main__":
    unittest.main()
