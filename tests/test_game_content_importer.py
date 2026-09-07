import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
from unittest.mock import Mock, patch

from tests.test_pregeneration_setup import write_story_index
from tests.test_pregeneration_voices import write_content
from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
    Reverse1999GameImporter,
    resolve_reverse1999_installation,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index


class FinishedProcess:
    def __init__(self, returncode=0, *, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class RunningProcess(FinishedProcess):
    def __init__(self):
        super().__init__(None)

    def communicate(self, timeout=None):
        self.returncode = -15
        return "", ""


class Reverse1999GameImporterTest(unittest.TestCase):
    def test_import_runs_bounded_command_and_consumes_shared_story_contract(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "imports"
            write_story_index(output / "reverse1999")
            process = FinishedProcess()
            popen = Mock(return_value=process)
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=output,
                popen_factory=popen,
            )

            content = importer.import_installed()

        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[0], "r1999-bootstrap")
        self.assertIn("--data-directory", arguments)
        self.assertIn(str(output), arguments)
        self.assertEqual(content.provider_id, "reverse1999")
        self.assertEqual(content.game, "Reverse: 1999")

    def test_failed_import_exposes_last_plain_error_line(self):
        process = FinishedProcess(
            2,
            stderr="details\nUnable to find installed English game audio\n",
        )
        with TemporaryDirectory() as temporary_directory:
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=temporary_directory,
                popen_factory=Mock(return_value=process),
            )

            with self.assertRaisesRegex(
                GameContentImportError,
                "Unable to find installed English game audio",
            ):
                importer.import_installed()

    def test_cancellation_terminates_only_the_owned_importer_process(self):
        process = RunningProcess()
        with TemporaryDirectory() as temporary_directory:
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=temporary_directory,
                popen_factory=Mock(return_value=process),
            )
            cancelled = Event()
            cancelled.set()

            with self.assertRaises(GameContentImportCancelled):
                importer.import_installed(cancelled)

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_missing_importer_is_reported_without_starting_a_process(self):
        importer = Reverse1999GameImporter(popen_factory=Mock())

        with patch.object(importer, "command", return_value=None):
            availability = importer.availability()
            with self.assertRaisesRegex(GameContentImportError, "not installed"):
                importer.import_installed()

        self.assertFalse(availability.available)
        importer.popen_factory.assert_not_called()

    def test_large_importer_output_is_drained_before_waiting_for_exit(self):
        cancelled = Event()
        timeout = Timer(5, cancelled.set)
        timeout.start()
        try:
            stdout, stderr = Reverse1999GameImporter()._run(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 1048576); "
                    "sys.stderr.write('y' * 1048576)",
                ),
                cancelled,
            )
        finally:
            timeout.cancel()
        self.assertEqual(stdout, "x" * 1048576)
        self.assertEqual(stderr, "y" * 1048576)

    def test_cancellation_interrupts_importer_while_communicating(self):
        cancelled = Event()
        timeout = Timer(0.2, cancelled.set)
        timeout.start()
        try:
            with self.assertRaises(GameContentImportCancelled):
                Reverse1999GameImporter()._run(
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    cancelled,
                )
        finally:
            timeout.cancel()

    def test_frozen_app_uses_its_hidden_provider_worker_entrypoint(self):
        importer = Reverse1999GameImporter()

        with (
            patch(
                "vntts.game_content_importer.importlib.util.find_spec",
                return_value=object(),
            ),
            patch.object(sys, "frozen", True, create=True),
        ):
            command = importer.command()

        self.assertEqual(
            command,
            (
                sys.executable,
                "--game-content-import-worker",
                "reverse1999",
            ),
        )

    def test_one_selected_installation_folder_resolves_all_importer_inputs(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "Reverse1999"
            resources = root / "ResLib" / "iOS"
            (resources / "bundles").mkdir(parents=True)
            configs = resources / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").touch()
            (configs / "language" / "json_language_en.json.dat").touch()
            audio = resources / "audios" / "iOS" / "en"
            audio.mkdir(parents=True)
            (audio / "activity.bnk").touch()

            resolved = resolve_reverse1999_installation(root)

        self.assertEqual(
            resolved,
            (resources.resolve(), configs.resolve(), audio.resolve()),
        )

    def test_incomplete_selected_installation_explains_missing_parts(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                GameContentImportError,
                "story bundles, game configuration, English voice banks",
            ):
                resolve_reverse1999_installation(temporary_directory)

    def test_prepares_candidates_only_for_selected_roles_with_source_audio(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(
                write_content(root / "content"),
                provider_id="reverse1999",
            )
            job = PregenerationJobStore(root / "jobs").create_or_resume(
                content,
                ("story",),
            )
            output = root / "imports"
            manifest = (
                output / "reverse1999" / "voice-candidates" / "id" / "manifest.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}", encoding="utf-8")
            process = FinishedProcess(
                stdout=json.dumps({"voice_manifest": str(manifest)}) + "\n"
            )
            popen = Mock(return_value=process)
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=output,
                popen_factory=popen,
            )

            with patch(
                "vntts.game_content_importer.ensure_game_decoder",
                return_value=root / "tools" / "vgmstream-cli",
            ):
                result = importer.prepare_voice_candidates(job)

        arguments = popen.call_args.args[0]
        self.assertEqual(result, manifest.resolve())
        self.assertIn("--prepare-voice-candidates-only", arguments)
        self.assertTrue(
            popen.call_args.kwargs["env"]["PATH"].startswith(str(root / "tools"))
        )
        roles = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--voice-candidate-role"
        ]
        self.assertEqual(roles, ["Rhiannon"])


if __name__ == "__main__":
    unittest.main()
