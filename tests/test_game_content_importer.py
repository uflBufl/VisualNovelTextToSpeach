import json
import shutil
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
    def setUp(self):
        config = TemporaryDirectory()
        self.addCleanup(config.cleanup)
        override = patch(
            "vntts.game_content_importer.get_config_directory",
            return_value=Path(config.name),
        )
        override.start()
        self.addCleanup(override.stop)

    def test_failed_import_records_saved_source_fallback_and_process_details(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            process = FinishedProcess(
                2,
                stdout="Importing installed game\n",
                stderr="config-candidate: datacfg_1.dat missing\n"
                "Unable to find installed game configs\n",
            )
            importer = Reverse1999GameImporter(
                output_root=output,
                command=("extractor",),
                popen_factory=Mock(return_value=process),
            )
            with patch("vntts.support.record_game_import") as record:
                with self.assertRaisesRegex(
                    GameContentImportError, "Unable to find installed game configs"
                ):
                    importer.import_installed()
            events = [(call.args[0], call.kwargs) for call in record.call_args_list]
            self.assertTrue(
                any(
                    stage == "saved-source" and details.get("exists") is False
                    for stage, details in events
                )
            )
            self.assertTrue(
                any(
                    stage == "saved-source"
                    and details.get("exception_type") == "FileNotFoundError"
                    for stage, details in events
                )
            )
            self.assertTrue(
                any(
                    stage == "import-roots"
                    and "auto-discovery" in details.get("reason", "")
                    for stage, details in events
                )
            )
            terminal = next(
                details for stage, details in events if stage == "process-exit"
            )
            self.assertEqual(terminal["exit_code"], 2)
            self.assertGreaterEqual(terminal["elapsed_ms"], 0)
            stderr = next(
                details for stage, details in events if stage == "process-stderr"
            )
            self.assertIn("datacfg_1.dat missing", stderr["stderr_tail"])

    def test_folder_diagnostics_identify_missing_required_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs" / "datacfg_1.dat").touch()
            with patch("vntts.support.record_game_import") as record:
                with self.assertRaises(GameContentImportError):
                    resolve_reverse1999_installation(root)
            candidate = next(
                call.kwargs
                for call in record.call_args_list
                if call.args[0] == "config-candidate"
            )
            self.assertEqual(
                candidate["missing"], ["language/json_language_en.json.dat"]
            )

    def test_narrator_decoder_uses_cancellable_subprocess_runner(self):
        cancellation = Event()
        cancellation.set()
        process = RunningProcess()
        importer = Reverse1999GameImporter(popen_factory=Mock(return_value=process))
        with self.assertRaises(GameContentImportCancelled):
            importer._decode_narrator(
                ["decoder", "-i", "source.wem"],
                capture_output=True,
                text=True,
                cancel_event=cancellation,
            )
        self.assertTrue(process.terminated)

    def test_narrator_listing_needs_no_decoder_and_selection_reaches_extractor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            importer = Reverse1999GameImporter(output_root=root, command=("extractor",))
            manifest = (
                root / "reverse1999" / "voice-candidates" / "one" / "manifest.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}")
            with (
                patch(
                    "r1999extractor.narrator_references.NarratorReferenceSession",
                ) as listing,
                patch(
                    "vntts.game_content_importer.ensure_game_decoder",
                    return_value=root / "decoder",
                ) as decoder,
                patch.object(
                    importer,
                    "_run",
                    return_value=(json.dumps({"voice_manifest": str(manifest)}), ""),
                ) as run,
            ):
                session = listing.return_value
                session.references = ("line",)
                session.role = "Centurion"
                session.prepare.return_value = manifest
                self.assertEqual(importer.narrator_references("Centurion"), ("line",))
                listing.assert_called_once_with(
                    root / "reverse1999" / "narrator-index.jsonl",
                    root / "reverse1999" / "english-bank-index.json",
                    "Centurion",
                    root / "reverse1999" / "voice-candidates",
                )
                decoder.assert_not_called()
                run.assert_not_called()
                importer.prepare_voice_roles(
                    ("Centurion",), narrator=True, narrator_line_id="playable:5"
                )
                self.assertEqual(
                    session.prepare.call_args.kwargs["line_id"], "playable:5"
                )
                self.assertEqual(
                    session.prepare.call_args.kwargs["decoder"], root / "decoder"
                )
                run.assert_not_called()

    def test_narrator_upgrade_reuses_previously_imported_custom_installation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            streaming_assets = root / "custom-drive" / "game" / "StreamingAssets"
            resources = streaming_assets / "PersistentRoot"
            (resources / "bundles").mkdir(parents=True)
            bundle = resources / "bundles" / "story.dat"
            bundle.touch()
            windows = streaming_assets / "Windows"
            (windows / "bundles").mkdir(parents=True)
            (windows / "bundles" / "other-story.dat").touch()
            configs = windows / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").touch()
            (configs / "language" / "json_language_en.json.dat").touch()
            audio = windows / "audios" / "Windows" / "en"
            audio.mkdir(parents=True)
            (audio / "hero.bnk").touch()
            output = root / "imports"
            story = write_content(output / "reverse1999")
            lines = story.read_text().splitlines()
            metadata = json.loads(lines[0])
            metadata["source_bundle"] = str(bundle)
            lines[0] = json.dumps(metadata)
            story.write_text("\n".join(lines) + "\n")
            # Existing narrator files do not mean the old single-folder scan is complete.
            (story.parent / "narrator-index.jsonl").write_text(story.read_text())
            (story.parent / "narrator-banks.json").write_text("{}")
            (story.parent / "english-bank-index.json").write_text(
                json.dumps(
                    {"version": 5, "game_audio_directory": str(audio), "banks": []}
                )
            )

            def finish_import(arguments, _cancel):
                self.assertEqual(
                    arguments[arguments.index("--resource-root") + 1], str(resources)
                )
                self.assertEqual(
                    arguments[arguments.index("--config-directory") + 1], str(configs)
                )
                self.assertEqual(
                    arguments[arguments.index("--game-audio-directory") + 1], str(audio)
                )
                (story.parent / "narrator-index.jsonl").write_text(story.read_text())
                (story.parent / "narrator-banks.json").write_text(
                    '{"Centurion": "hero.bnk"}'
                )
                from r1999extractor.reverse1999_index import build_bank_index

                build_bank_index(audio, output=story.parent / "english-bank-index.json")

            importer = Reverse1999GameImporter(
                command=("extractor",), output_root=output
            )
            with patch.object(importer, "_run", side_effect=finish_import) as run:
                self.assertIn("Centurion", importer.narrator_characters())
                self.assertIn("Centurion", importer.narrator_characters())
                run.assert_called_once()
                (story.parent / "english-bank-index.json").unlink()
                self.assertIn("Centurion", importer.narrator_characters())
                self.assertEqual(run.call_count, 2)

            # A fresh importer can recover after the entire disposable tree is removed.
            saved_story = story.read_text()
            shutil.rmtree(output)
            restarted = Reverse1999GameImporter(
                command=("extractor",), output_root=output
            )

            def reimport(arguments, cancel):
                story.parent.mkdir(parents=True)
                story.write_text(saved_story)
                finish_import(arguments, cancel)

            with patch.object(restarted, "_run", side_effect=reimport):
                self.assertIn("Centurion", restarted.narrator_characters())

    def test_explicit_and_automatic_imports_remember_validated_roots(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                resources = root / "game"
                (resources / "bundles").mkdir(parents=True)
                bundle = resources / "bundles" / "story.dat"
                bundle.touch()
                configs = resources / "configs"
                (configs / "language").mkdir(parents=True)
                (configs / "datacfg_1.dat").touch()
                (configs / "language/json_language_en.json.dat").touch()
                audio = resources / "en"
                audio.mkdir()
                (audio / "hero.bnk").touch()
                output = root / "imports"
                saved = root / "config" / "installation.json"
                importer = Reverse1999GameImporter(
                    command=("extractor",), output_root=output, installation_file=saved
                )

                def finish(arguments, cancel):
                    story = write_content(output / "reverse1999")
                    lines = story.read_text().splitlines()
                    metadata = json.loads(lines[0])
                    metadata["source_bundle"] = str(bundle)
                    story.write_text(
                        "\n".join([json.dumps(metadata), *lines[1:]]) + "\n"
                    )

                with patch.object(importer, "_run", side_effect=finish):
                    importer.import_installed(
                        installation_root=resources if explicit else None
                    )
                self.assertTrue(saved.is_file())
                shutil.rmtree(output)
                restarted = Reverse1999GameImporter(
                    output_root=output, installation_file=saved
                )
                self.assertEqual(
                    restarted._previous_installation(), (resources, configs, audio)
                )
                # Removed game sources and malformed settings must allow fresh discovery.
                bundle.unlink()
                (configs / "datacfg_1.dat").unlink()
                self.assertIsNone(restarted._previous_installation())
                for malformed in ("null", "[]", "{}", "not json"):
                    saved.write_text(malformed)
                    self.assertIsNone(restarted._previous_installation())

    def test_failed_or_cancelled_selection_preserves_last_successful_installation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "installation.json"
            saved.write_text('{"previous": "selection"}')
            importer = Reverse1999GameImporter(
                command=("extractor",),
                output_root=root / "imports",
                installation_file=saved,
            )
            for error in (
                OSError("startup"),
                GameContentImportError("failed"),
                GameContentImportCancelled("cancelled"),
            ):
                with (
                    self.subTest(error=error),
                    patch(
                        "vntts.game_content_importer.resolve_reverse1999_installation",
                        return_value=(root, root, root),
                    ),
                    patch.object(importer, "_run", side_effect=error),
                ):
                    with self.assertRaises(type(error)):
                        importer.import_installed(installation_root=root)
                    self.assertEqual(saved.read_text(), '{"previous": "selection"}')

    def test_selected_windows_child_keeps_its_bundle_when_persistent_root_also_has_one(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            streaming_assets = Path(temporary_directory) / "StreamingAssets"
            persistent_root = streaming_assets / "PersistentRoot"
            (persistent_root / "bundles").mkdir(parents=True)
            (persistent_root / "bundles" / "persistent.dat").touch()
            windows = streaming_assets / "Windows"
            (windows / "bundles").mkdir(parents=True)
            (windows / "bundles" / "windows.dat").touch()
            configs = persistent_root / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").touch()
            (configs / "language" / "json_language_en.json.dat").touch()
            audio = persistent_root / "audios" / "Windows" / "en"
            audio.mkdir(parents=True)
            (audio / "activity.bnk").touch()

            resolved = resolve_reverse1999_installation(windows)

        self.assertEqual(
            resolved,
            (windows.resolve(), configs.resolve(), audio.resolve()),
        )

    def test_selected_persistent_root_ignores_unrelated_streaming_assets_sibling(self):
        with TemporaryDirectory() as temporary_directory:
            streaming_assets = Path(temporary_directory) / "StreamingAssets"
            persistent_root = streaming_assets / "PersistentRoot"
            (persistent_root / "bundles").mkdir(parents=True)
            (persistent_root / "bundles" / "story.dat").touch()
            unrelated = streaming_assets / "Unrelated"
            configs = unrelated / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").touch()
            (configs / "language" / "json_language_en.json.dat").touch()
            audio = unrelated / "audios" / "Windows" / "en"
            audio.mkdir(parents=True)
            (audio / "activity.bnk").touch()

            with self.assertRaisesRegex(
                GameContentImportError,
                "game configuration, English voice banks",
            ):
                resolve_reverse1999_installation(persistent_root)

    def test_unusable_previous_source_does_not_block_automatic_import(self):
        with TemporaryDirectory() as directory:
            story = write_content(Path(directory) / "reverse1999")
            lines = story.read_text().splitlines()
            importer = Reverse1999GameImporter(
                command=("extractor",), output_root=directory
            )
            for source in (
                None,
                "",
                str(Path(directory) / "removed" / "bundles" / "x"),
            ):
                with self.subTest(source=source):
                    metadata = json.loads(lines[0])
                    metadata["source_bundle"] = source
                    story.write_text(
                        "\n".join([json.dumps(metadata), *lines[1:]]) + "\n"
                    )
                    with patch.object(importer, "_run") as run:
                        importer.import_installed()
                    self.assertNotIn("--resource-root", run.call_args.args[0])

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
