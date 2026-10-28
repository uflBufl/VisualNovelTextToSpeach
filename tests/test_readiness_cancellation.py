import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from vntts.onboarding import OnboardingDiagnostics, probe_tesseract
from vntts.runtime_preparation import RuntimePreparationCancelled
from vntts.rust_audio import AudioRuntimeUnavailable
from vntts.settings import AppSettings


class ReadinessCancellationTest(unittest.TestCase):
    def setUp(self):
        self.display_target = self.enterContext(
            patch("vntts.onboarding.DisplayCaptureTarget")
        )
        self.window_target = self.enterContext(
            patch("vntts.onboarding.WindowCaptureTarget")
        )

    def settings(self):
        return AppSettings(
            speech_backend="coqui-xtts", tts_model="test", narrator_speaker="narrator"
        )

    def diagnostics(self, **overrides):
        probes = {
            "tesseract_probe": Mock(return_value="5.5"),
            "audio_probe": Mock(return_value="Speakers"),
            "model_path_resolver": Mock(return_value=Path.cwd()),
            "permission_status_provider": Mock(
                return_value={"screen_capture": True, "accessibility": True}
            ),
        }
        probes.update(overrides)
        return OnboardingDiagnostics(**probes)

    def test_pre_cancelled_readiness_runs_no_probes(self):
        cancelled = Event()
        cancelled.set()
        diagnostics = self.diagnostics()
        with (
            patch.object(diagnostics, "_check_hotkeys") as hotkeys,
            self.assertRaises(RuntimePreparationCancelled),
        ):
            diagnostics.run(self.settings(), cancellation=cancelled)
        hotkeys.assert_not_called()
        self.display_target.assert_not_called()
        self.window_target.assert_not_called()
        diagnostics.tesseract_probe.assert_not_called()
        diagnostics.audio_probe.assert_not_called()
        diagnostics.model_path_resolver.assert_not_called()
        diagnostics.permission_status_provider.assert_not_called()

    def test_cancellation_during_failed_probe_does_not_log_installation_error(self):
        for probe_name, failure in (
            ("tesseract_probe", RuntimeError("OCR service is shutting down")),
            ("audio_probe", AudioRuntimeUnavailable("Audio service is shutting down")),
            ("audio_probe", RuntimeError("Audio service is shutting down")),
            ("model_path_resolver", RuntimeError("Runtime is shutting down")),
        ):
            with self.subTest(probe=probe_name, failure=type(failure).__name__):
                cancelled = Event()

                def fail(*args):
                    cancelled.set()
                    raise failure

                diagnostics = self.diagnostics(**{probe_name: Mock(side_effect=fail)})
                with (
                    self.assertNoLogs("vntts.onboarding", level="ERROR"),
                    self.assertRaises(RuntimePreparationCancelled),
                ):
                    diagnostics.run(self.settings(), cancellation=cancelled)
                diagnostics.permission_status_provider.assert_not_called()

    def test_cancelled_successful_probe_stops_before_next_probe(self):
        cancelled = Event()
        diagnostics = self.diagnostics(
            tesseract_probe=Mock(side_effect=lambda: cancelled.set())
        )
        with self.assertRaises(RuntimePreparationCancelled):
            diagnostics.run(self.settings(), cancellation=cancelled.is_set)
        diagnostics.tesseract_probe.assert_called_once_with()
        diagnostics.audio_probe.assert_not_called()

    def test_default_native_probe_receives_the_readiness_cancellation_callback(self):
        for token_kind in ("event", "callable"):
            with self.subTest(token_kind=token_kind):
                cancelled = Event()
                token = cancelled if token_kind == "event" else cancelled.is_set

                def recognize(_image, **options):
                    self.assertFalse(options["cancelled"]())
                    cancelled.set()
                    self.assertTrue(options["cancelled"]())
                    raise RuntimeError("OCR service is shutting down")

                diagnostics = self.diagnostics(tesseract_probe=None)
                with (
                    patch("pytesseract.get_tesseract_version", return_value="5.5"),
                    patch(
                        "vntts.ocr_worker.recognize_dialog_image_result",
                        side_effect=recognize,
                    ) as native,
                    patch("vntts.ocr_worker.open_ocr_worker") as reopen,
                    self.assertNoLogs("vntts.onboarding", level="ERROR"),
                    self.assertRaises(RuntimePreparationCancelled),
                ):
                    diagnostics.run(self.settings(), cancellation=token)
                native.assert_called_once()
                reopen.assert_not_called()
                diagnostics.audio_probe.assert_not_called()

    def test_cancelled_version_probe_does_not_enter_native_ocr(self):
        cancelled = Event()
        with (
            patch(
                "pytesseract.get_tesseract_version", side_effect=lambda: cancelled.set()
            ),
            patch("vntts.ocr_worker.recognize_dialog_image_result") as native,
            self.assertRaises(RuntimePreparationCancelled),
        ):
            probe_tesseract(cancellation=cancelled)
        native.assert_not_called()

    def test_genuine_uncancelled_ocr_failure_is_reported_and_logged(self):
        failure = RuntimeError("Tesseract executable was not found")
        diagnostics = self.diagnostics(tesseract_probe=Mock(side_effect=failure))
        with self.assertLogs("vntts.onboarding", level="ERROR") as logged:
            results = diagnostics.run(self.settings(), cancellation=Event())
        result = next(result for result in results if result.name == "Tesseract OCR")
        self.assertEqual((result.status, result.remediation), ("error", "external-ocr"))
        self.assertEqual(result.message, str(failure))
        self.assertIs(logged.records[0].exc_info[1], failure)
        diagnostics.tesseract_probe.assert_called_once_with()
        diagnostics.audio_probe.assert_called_once_with()
        self.display_target.return_value.capture_native.assert_called_once()
        self.display_target.return_value.close.assert_called_once_with()

    def test_setup_passes_its_cancellation_to_read_only_diagnostics(self):
        diagnostics = self.diagnostics()
        settings = self.settings()
        cancelled = Event()
        with patch.object(diagnostics, "run", return_value=()) as run:
            self.assertEqual(
                diagnostics.prepare_and_run(
                    settings, cancellation=cancelled, progress=Mock()
                ),
                (),
            )
        run.assert_called_once_with(settings, cancellation=cancelled)
