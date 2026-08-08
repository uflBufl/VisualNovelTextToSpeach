import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from vntts.main import initialize_tts, main


class MainTest(unittest.TestCase):
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
