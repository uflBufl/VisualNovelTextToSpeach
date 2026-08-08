import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from vntts.main import (
    OCRError,
    ScreenCaptureError,
    capture_dialog,
    create_dialog_read_scheduler,
    initialize_tts,
    main,
    read_dialog_safely,
    recognize_screenshot,
)
from vntts.services.tts_engine import AudioPlaybackError, TTSSynthesisError


class MainTest(unittest.TestCase):
    def test_capture_failure_identifies_screen_capture_stage(self):
        with patch(
            'vntts.main.mss.mss',
            side_effect=RuntimeError('display unavailable'),
        ):
            with self.assertRaisesRegex(ScreenCaptureError, 'display unavailable'):
                capture_dialog()

    def test_ocr_failure_identifies_tesseract_stage(self):
        with patch(
            'vntts.main.recognize_dialog',
            side_effect=RuntimeError('tesseract unavailable'),
        ):
            with self.assertRaisesRegex(OCRError, 'tesseract unavailable'):
                recognize_screenshot(object())

    def test_runtime_failures_are_reported_by_stage(self):
        failures = [
            (ScreenCaptureError('no display'), 'Screen capture failed: no display'),
            (OCRError('ocr crashed'), 'Tesseract OCR failed: ocr crashed'),
            (
                TTSSynthesisError('model crashed'),
                'TTS model or synthesis failed: model crashed',
            ),
            (
                AudioPlaybackError('device lost'),
                'Audio playback failed: device lost',
            ),
        ]

        for error, expected_message in failures:
            with self.subTest(error=error):
                errors = io.StringIO()
                with (
                    redirect_stderr(errors),
                    patch('vntts.main.read_dialog', side_effect=error),
                ):
                    read_dialog_safely(Mock())

                self.assertEqual(errors.getvalue().strip(), expected_message)

    def test_scheduler_allows_retry_after_failed_job_finishes(self):
        executor = Mock()
        failed_job = Mock()
        failed_job.done.return_value = True
        retry_job = Mock()
        executor.submit.side_effect = [failed_job, retry_job]
        tts = Mock()
        schedule_dialog_read = create_dialog_read_scheduler(executor, tts)

        schedule_dialog_read()
        schedule_dialog_read()

        self.assertEqual(executor.submit.call_count, 2)
        executor.submit.assert_called_with(read_dialog_safely, tts)

    def test_initialize_tts_reports_loading_progress(self):
        tts = object()
        tts_factory = Mock(return_value=tts)
        output = io.StringIO()

        with redirect_stdout(output):
            result = initialize_tts(tts_factory)

        self.assertIs(result, tts)
        tts_factory.assert_called_once_with()
        self.assertEqual(
            output.getvalue().splitlines(),
            ['Loading TTS model...', 'TTS model loaded'],
        )

    def test_main_reports_tts_failure_without_starting_listener(self):
        tts_factory = Mock(side_effect=RuntimeError('model unavailable'))
        output = io.StringIO()
        errors = io.StringIO()

        with (
            redirect_stdout(output),
            redirect_stderr(errors),
            patch('vntts.main.listen_for_hotkey') as listen_for_hotkey,
        ):
            result = main(tts_factory)

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), 'Loading TTS model...\n')
        self.assertIn(
            'Unable to initialize TTS engine: model unavailable',
            errors.getvalue(),
        )
        listen_for_hotkey.assert_not_called()

    def test_main_passes_initialized_tts_to_dialog_scheduler(self):
        tts = object()
        tts_factory = Mock(return_value=tts)
        schedule_dialog_read = Mock()
        callback = Mock()
        schedule_dialog_read.return_value = callback
        executor = Mock()

        with (
            redirect_stdout(io.StringIO()),
            patch('vntts.main.get_hotkey', return_value='<ctrl>+h'),
            patch('vntts.main.ThreadPoolExecutor') as executor_factory,
            patch(
                'vntts.main.create_dialog_read_scheduler',
                schedule_dialog_read,
            ),
            patch('vntts.main.listen_for_hotkey') as listen_for_hotkey,
        ):
            executor_factory.return_value.__enter__.return_value = executor
            result = main(tts_factory)

        self.assertEqual(result, 0)
        schedule_dialog_read.assert_called_once_with(executor, tts)
        listen_for_hotkey.assert_called_once_with('<ctrl>+h', callback)


if __name__ == '__main__':
    unittest.main()
