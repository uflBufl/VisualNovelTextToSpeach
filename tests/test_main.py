import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.main import (
    OCRError,
    ScreenCaptureError,
    capture_dialog,
    create_screenshot_path,
    create_dialog_read_scheduler,
    get_screenshot_directory,
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

    def test_capture_creates_configured_directory_and_saves_rgb_image(self):
        screenshot = Mock(size=(1, 1), bgra=b'\x00\x00\x00\xff')
        screen = Mock()
        screen.monitors = [None, {'height': 100, 'width': 100}]
        screen.grab.return_value = screenshot

        with TemporaryDirectory() as temporary_directory:
            screenshot_directory = Path(temporary_directory) / 'nested'
            with patch('vntts.main.mss.mss') as mss_factory:
                mss_factory.return_value.__enter__.return_value = screen
                image, output = capture_dialog(screenshot_directory)

            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(output.parent, screenshot_directory)
            self.assertTrue(output.is_file())

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
                    read_dialog_safely(Mock(), Path('captures'))

                self.assertEqual(errors.getvalue().strip(), expected_message)

    def test_scheduler_allows_retry_after_failed_job_finishes(self):
        executor = Mock()
        failed_job = Mock()
        failed_job.done.return_value = True
        retry_job = Mock()
        executor.submit.side_effect = [failed_job, retry_job]
        tts = Mock()
        screenshot_directory = Path('captures')
        schedule_dialog_read = create_dialog_read_scheduler(
            executor,
            tts,
            screenshot_directory,
        )

        schedule_dialog_read()
        schedule_dialog_read()

        self.assertEqual(executor.submit.call_count, 2)
        executor.submit.assert_called_with(
            read_dialog_safely,
            tts,
            screenshot_directory,
        )

    def test_screenshot_directory_is_configurable(self):
        with patch.dict(
            'os.environ',
            {'VNTTS_SCREENSHOT_DIR': 'custom/captures'},
        ):
            self.assertEqual(
                get_screenshot_directory(),
                Path('custom/captures'),
            )

    def test_screenshot_names_do_not_collide_within_one_second(self):
        first_id = Mock(hex='first')
        second_id = Mock(hex='second')
        timestamp = Mock()
        timestamp.strftime.return_value = '2026-08-08-12-00-00'

        with (
            patch('vntts.main.datetime') as datetime_module,
            patch('vntts.main.uuid4', side_effect=[first_id, second_id]),
        ):
            datetime_module.now.return_value = timestamp
            first = create_screenshot_path(Path('captures'))
            second = create_screenshot_path(Path('captures'))

        self.assertEqual(
            first,
            Path('captures/dialog-2026-08-08-12-00-00-first.png'),
        )
        self.assertEqual(
            second,
            Path('captures/dialog-2026-08-08-12-00-00-second.png'),
        )
        self.assertNotEqual(first, second)

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
        screenshot_directory = Path('custom/captures')

        with (
            redirect_stdout(io.StringIO()),
            patch('vntts.main.get_hotkey', return_value='<ctrl>+h'),
            patch(
                'vntts.main.get_screenshot_directory',
                return_value=screenshot_directory,
            ),
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
        schedule_dialog_read.assert_called_once_with(
            executor,
            tts,
            screenshot_directory,
        )
        listen_for_hotkey.assert_called_once_with('<ctrl>+h', callback)


if __name__ == '__main__':
    unittest.main()
