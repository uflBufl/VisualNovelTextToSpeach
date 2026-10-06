import hashlib
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
from unittest.mock import Mock, patch

from vntts_artifacts import load_story_index_document, write_story_index_document
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from tests.symlink_support import symlink_or_skip
from tests.test_pregeneration_setup import write_story_index
from tests.test_pregeneration_voices import write_content
from vntts.document_identity import canonical_document_sha256
from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
    Reverse1999GameImporter,
    _cached_playable_voice_roles,
    resolve_reverse1999_installation,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.source_audio_semantics import SEMANTIC_EVIDENCE_METHOD, semantic_text_sha256


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

    def test_installed_story_change_check_reuses_saved_file_signatures(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            resources = root / "game"
            bundle = resources / "bundles" / "story.dat"
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(b"old story")
            configs = root / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").write_bytes(b"old config")
            (configs / "language/json_language_en.json.dat").write_bytes(b"old text")
            audio = root / "audio"
            audio.mkdir()
            (audio / "hero.bnk").touch()
            story = write_content(root / "import" / "reverse1999")
            lines = story.read_text().splitlines()
            metadata = json.loads(lines[0])
            metadata["source_bundle"] = str(bundle.resolve())
            lines[0] = json.dumps(metadata)
            story.write_text("\n".join(lines) + "\n")
            importer = Reverse1999GameImporter(output_root=root / "import")
            roots = (resources, configs, audio)
            importer._remember_installation(roots)
            bank_index = story.parent / "english-bank-index.json"

            with patch.object(importer, "_bank_index_is_stale", return_value=False):
                self.assertTrue(importer.installed_story_changed())
                bank_index.write_text("{}")
                self.assertFalse(importer.installed_story_changed())
                bundle.write_bytes(b"newer story content")
                self.assertTrue(importer.installed_story_changed())
                importer._remember_story_inputs(story, roots)
                self.assertFalse(importer.installed_story_changed())
                (configs / "datacfg_1.dat").write_bytes(b"new config")
                self.assertTrue(importer.installed_story_changed())
                importer._remember_story_inputs(story, roots)
            with patch.object(importer, "_bank_index_is_stale", return_value=True):
                self.assertTrue(importer.installed_story_changed())

    def test_story_change_check_handles_nonobject_source_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "reverse1999" / "story-index.jsonl"
            story.parent.mkdir()
            importer = Reverse1999GameImporter(output_root=root)
            with patch.object(
                importer, "_previous_installation", return_value=(root, root, root)
            ):
                for metadata in ([], None, True, 1, "metadata"):
                    with self.subTest(metadata=metadata):
                        story.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
                        self.assertFalse(importer.installed_story_changed())

    def test_source_signatures_reject_noninteger_versions_and_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            story.write_bytes(b"x")
            inputs = tuple(root / f"input-{index}" for index in range(3))
            for path in inputs:
                path.write_bytes(b"x")
            importer = Reverse1999GameImporter(output_root=root)
            saved = {
                "version": 1,
                "story_index": importer._file_signature(story),
                "inputs": {
                    str(path): importer._file_signature(path) for path in inputs
                },
            }
            self.assertFalse(importer._saved_story_inputs_changed(story, saved))
            for version in (True, 1.0):
                with self.subTest(version=version):
                    self.assertTrue(
                        importer._saved_story_inputs_changed(
                            story, saved | {"version": version}
                        )
                    )
            for target in ("story_index", "inputs"):
                for count in (True, 1.0):
                    with self.subTest(target=target, count=count):
                        malformed = json.loads(json.dumps(saved))
                        signature = (
                            malformed["story_index"]
                            if target == "story_index"
                            else malformed["inputs"][str(inputs[0])]
                        )
                        signature[0] = count
                        self.assertTrue(
                            importer._saved_story_inputs_changed(story, malformed)
                        )

    def test_optional_voice_caches_require_integer_versions(self):
        for version in (1, True, 1.0):
            with self.subTest(version=version), TemporaryDirectory() as directory:
                root = Path(directory)
                story = write_story_index(root)
                banks = root / "narrator-banks.json"
                banks.write_text('["Rhiannon"]', encoding="utf-8")
                checksum = sha256_file(story)
                atomic_write_json(
                    root / "playable-voice-roles.json",
                    {"version": version, "index_sha256": checksum, "roles": ["cached"]},
                )
                with self.subTest(cache="playable"):
                    self.assertEqual(
                        _cached_playable_voice_roles(story),
                        {"cached"} if type(version) is int else {"centurion"},
                    )
                atomic_write_json(
                    root / "narrator-characters.json",
                    {
                        "version": version,
                        "story_index_sha256": checksum,
                        "narrator_banks_sha256": sha256_file(banks),
                        "characters": ["Cached"],
                    },
                )
                names = Reverse1999GameImporter._cached_narrator_characters(
                    story, banks, checksum, sha256_file(banks)
                )
                self.assertEqual(
                    names,
                    ("Cached",) if type(version) is int else ("Centurion", "Rhiannon"),
                )

    def test_narrator_cache_rejects_changed_inputs_before_publication(self):
        for changed_source in ("story", "banks"):
            with (
                self.subTest(changed_source=changed_source),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                story = write_story_index(root)
                banks = root / "narrator-banks.json"
                banks.write_text('["Rhiannon"]', encoding="utf-8")
                index_sha256 = sha256_file(story)
                banks_sha256 = sha256_file(banks)
                if changed_source == "story":
                    story.write_text(
                        story.read_text(encoding="utf-8").replace(
                            "Centurion", "Changed"
                        ),
                        encoding="utf-8",
                    )
                else:
                    banks.write_text('["Changed"]', encoding="utf-8")
                with self.assertRaisesRegex(
                    (GameContentImportError, ValueError), "changed"
                ):
                    Reverse1999GameImporter._cached_narrator_characters(
                        story, banks, index_sha256, banks_sha256
                    )
                self.assertFalse((root / "narrator-characters.json").exists())

    def test_narrator_checksum_disappearance_uses_import_error_boundary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "reverse1999"
            story = write_story_index(output)
            (output / "narrator-index.jsonl").write_bytes(story.read_bytes())
            (output / "narrator-banks.json").write_text("[]", encoding="utf-8")
            (output / "english-bank-index.json").write_text("{}", encoding="utf-8")
            importer = Reverse1999GameImporter(output_root=root)
            with (
                patch.object(
                    importer,
                    "_bank_index_is_stale",
                    side_effect=lambda _path: (
                        (output / "narrator-index.jsonl").unlink(),
                        False,
                    )[1],
                ),
            ):
                with self.assertRaises(GameContentImportError) as raised:
                    importer.narrator_characters()
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_precancelled_import_does_not_start_a_process(self):
        with TemporaryDirectory() as directory:
            process_factory = Mock(return_value=RunningProcess())
            importer = Reverse1999GameImporter(
                command=("worker",),
                output_root=directory,
                popen_factory=process_factory,
            )
            cancelled = Event()
            cancelled.set()
            with self.assertRaises(GameContentImportCancelled):
                importer.import_installed(cancelled)
            process_factory.assert_not_called()

    def test_prepares_selected_stage_semantics_as_an_immutable_successor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "content" / "story-index.jsonl"
            source.parent.mkdir(parents=True)
            text = "Original game voice."
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            write_story_index_document(
                source,
                {"game": "Reverse: 1999", "language": "en"},
                [
                    {
                        "record_type": "line",
                        "line_id": "voice:1",
                        "chapter": "314501",
                        "sequence": 1,
                        "speaker": "A",
                        "text": text,
                        "text_sha256": text_hash,
                        "kind": "dialogue",
                        "source_audio_status": "available",
                        "source_bank": "voice.bnk",
                        "source_media_ids": [7],
                        "available_media_ids": [7],
                    },
                    {
                        "record_type": "line",
                        "line_id": "voice:untimed",
                        "chapter": "314501",
                        "sequence": 2,
                        "speaker": "A",
                        "text": "Cue without an exact media route.",
                        "kind": "dialogue",
                        "source_audio_status": "available",
                        "source_bank": "voice.bnk",
                        "source_media_ids": [8],
                        "available_media_ids": [],
                    },
                ],
            )
            content = inspect_story_index(source, provider_id="reverse1999")
            job = PregenerationJobStore(root / "jobs").create_or_resume(
                content, ("chapter:314501",)
            )
            importer = Reverse1999GameImporter(output_root=root / "imports")
            bank_index = (
                importer.output_root / "reverse1999" / "english-bank-index.json"
            )
            bank_index.parent.mkdir(parents=True)
            bank_index.write_text("{}", encoding="utf-8")
            model = root / "model"
            model.mkdir()
            original = source.read_bytes()

            def publish(arguments, _cancel_event):
                if "--evidence-output" not in arguments:
                    record = {
                        **load_story_index_document(source).records[0].to_record(),
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_duration_media_id": 7,
                        "source_audio_duration_media_sha256": "a" * 64,
                        "source_audio_duration_sample_rate": 24000,
                        "source_audio_duration_sample_count": 24000,
                        "source_audio_duration_decoder": "test",
                        "source_audio_completeness": "unknown",
                        "source_audio_completeness_reason": (
                            "duration-plausible-but-semantic-coverage-unverified"
                        ),
                    }
                    untimed = load_story_index_document(source).records[1].to_record()
                    write_story_index_document(
                        Path(arguments[arguments.index("--output") + 1]),
                        {
                            "game": "Reverse: 1999",
                            "language": "en",
                            "source_audio_completion": "verified-media-duration-seconds",
                        },
                        [record, untimed],
                    )
                    return "", ""
                timed = Path(arguments[arguments.index("--story-index") + 1])
                record = load_story_index_document(timed).records[0].to_record()
                untimed = load_story_index_document(timed).records[1].to_record()
                entry = {
                    "locale": "en",
                    "media_sha256": "a" * 64,
                    "displayed_text_sha256": text_hash,
                    "normalized_displayed_text_sha256": semantic_text_sha256(text),
                    "observed_transcript": text,
                    "normalized_observed_text_sha256": semantic_text_sha256(text),
                    "verdict": "full",
                    "reason": "exact-normalized-asr-transcript",
                    "method": SEMANTIC_EVIDENCE_METHOD,
                    "model_sha256": "b" * 64,
                    "source_line_ids": ["voice:1"],
                }
                entry["entry_id"] = canonical_document_sha256(
                    {
                        key: value
                        for key, value in entry.items()
                        if key != "source_line_ids"
                    }
                )
                evidence = {
                    "schema": "r1999.source-audio-semantic-evidence",
                    "schema_version": 1,
                    "locale": "en",
                    "source_story_index_sha256": "c" * 64,
                    "model": {
                        "kind": "whisper",
                        "snapshot": "test",
                        "sha256": "b" * 64,
                        "device": "cpu",
                        "decoding": "deterministic_greedy_default",
                    },
                    "entries": [entry],
                }
                evidence["evidence_id"] = canonical_document_sha256(evidence)
                evidence["generated_at"] = "2026-09-29T00:00:00+00:00"
                evidence_path = Path(
                    arguments[arguments.index("--evidence-output") + 1]
                )
                atomic_write_json(evidence_path, evidence, sort_keys=True)
                record.update(
                    source_audio_completeness="full",
                    source_audio_completeness_reason=(
                        "exact-normalized-asr-transcript"
                    ),
                    source_audio_semantic_evidence_id=evidence["evidence_id"],
                    source_audio_semantic_evidence_entry_id=entry["entry_id"],
                )
                write_story_index_document(
                    Path(arguments[arguments.index("--story-output") + 1]),
                    {
                        "game": "Reverse: 1999",
                        "language": "en",
                        "source_audio_completion": "verified-media-duration-seconds",
                        "source_audio_semantics": {
                            "evidence_id": evidence["evidence_id"],
                            "evidence_sha256": sha256_file(evidence_path),
                            "method": SEMANTIC_EVIDENCE_METHOD,
                            "selected_chapters": ["314501"],
                            "applied_count": 1,
                        },
                    },
                    [record, untimed],
                )
                return "", ""

            with (
                patch(
                    "vntts.game_content_importer.ensure_game_decoder",
                    return_value=root / "vgmstream-cli",
                ),
                patch.object(importer, "_publisher_command", return_value=("worker",)),
                patch.object(importer, "_resolve_asr_model", return_value=model),
                patch.object(importer, "_run", side_effect=publish) as run,
            ):
                successor = importer.prepare_source_audio_semantics(job)

            self.assertEqual(source.read_bytes(), original)
            self.assertNotEqual(successor.story_index, source)
            self.assertEqual(successor.selections[0].original_audio_lines, 1)
            self.assertEqual(successor.selections[0].generation_lines, 1)
            self.assertFalse(
                (successor.story_index.parent / "timed-story-index.jsonl").exists()
            )
            semantic_arguments = run.call_args_list[1].args[0]
            duration_arguments = run.call_args_list[0].args[0]
            shared_cache = str(
                importer.output_root / "reverse1999" / "source-audio-analysis-cache"
            )
            for arguments in (duration_arguments, semantic_arguments):
                self.assertEqual(
                    arguments[arguments.index("--cache-dir") + 1], shared_cache
                )
            self.assertEqual(
                semantic_arguments[semantic_arguments.index("--chapter") + 1],
                "314501",
            )

    def test_failed_update_restores_previous_story_catalog(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_content(root / "import" / "reverse1999")
            original = story.read_bytes()
            importer = Reverse1999GameImporter(
                command=("extractor",), output_root=root / "import"
            )

            def fail_after_story_write(_arguments, _cancel_event):
                replacement = story.with_suffix(".new")
                replacement.write_bytes(b"partial new catalog")
                os.replace(replacement, story)
                raise GameContentImportError("later importer step failed")

            with patch.object(importer, "_run", side_effect=fail_after_story_write):
                with self.assertRaisesRegex(GameContentImportError, "later"):
                    importer.import_installed()
            self.assertEqual(story.read_bytes(), original)

    def test_failed_first_import_removes_rejected_story_catalog(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "import"
            story = output / "reverse1999" / "story-index.jsonl"
            importer = Reverse1999GameImporter(
                command=("extractor",), output_root=output
            )

            def fail_after_story_write(_arguments, _cancel_event):
                story.parent.mkdir(parents=True)
                story.write_bytes(b"partial new catalog")
                raise GameContentImportError("later importer step failed")

            with patch.object(importer, "_run", side_effect=fail_after_story_write):
                with self.assertRaisesRegex(GameContentImportError, "later"):
                    importer.import_installed()
            self.assertFalse(story.exists())

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

    def test_installation_search_skips_bundle_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundles" / "decoy" / "configs").mkdir(parents=True)
            resources = root / "content"
            configs = resources / "configs"
            (configs / "language").mkdir(parents=True)
            (configs / "datacfg_1.dat").touch()
            (configs / "language" / "json_language_en.json.dat").touch()
            audio = resources / "audios" / "en"
            audio.mkdir(parents=True)
            (audio / "voice.bnk").touch()

            with patch("vntts.support.record_game_import") as record:
                resolved = resolve_reverse1999_installation(root)

            self.assertEqual(
                resolved, (root.resolve(), configs.resolve(), audio.resolve())
            )
            result = next(
                call.kwargs
                for call in record.call_args_list
                if call.args[0] == "folder-result"
            )
            self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_narrator_decoder_uses_cancellable_subprocess_runner(self):
        cancellation = Event()
        process = RunningProcess()

        def start_process(*args, **kwargs):
            cancellation.set()
            return process

        importer = Reverse1999GameImporter(
            popen_factory=Mock(side_effect=start_process)
        )
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
                self.assertEqual(restarted.selected_installation_root(), resources)
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
        cancelled = Event()

        def start_process(*args, **kwargs):
            cancelled.set()
            return process

        with TemporaryDirectory() as temporary_directory:
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=temporary_directory,
                popen_factory=Mock(side_effect=start_process),
            )
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

    def test_communication_failure_reaps_owned_importer(self):
        class BrokenPipeProcess(RunningProcess):
            def communicate(self, timeout=None):
                if not self.terminated:
                    raise OSError("importer pipe failed")
                self.returncode = -15
                return "", ""

        process = BrokenPipeProcess()
        importer = Reverse1999GameImporter(popen_factory=Mock(return_value=process))

        with self.assertRaisesRegex(OSError, "importer pipe failed"):
            importer._run(("worker",), None)

        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, -15)

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

    def test_frozen_app_uses_hidden_source_audio_publisher_worker(self):
        for worker in ("duration", "semantics"):
            for conflicting_executable in (None, f"/outside/r1999-{worker}"):
                with (
                    self.subTest(worker=worker, executable=conflicting_executable),
                    patch(
                        "vntts.game_content_importer.shutil.which",
                        return_value=conflicting_executable,
                    ),
                    patch.object(sys, "frozen", True, create=True),
                ):
                    command = Reverse1999GameImporter._publisher_command(
                        f"r1999-source-audio-{worker}",
                        f"r1999extractor.source_audio_{worker}",
                    )
                    self.assertEqual(
                        command,
                        (sys.executable, "--source-audio-publisher-worker", worker),
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
                provider_id="selected-story-index",
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
        self.assertEqual(
            arguments[arguments.index("--target-story-index") + 1],
            str(job.story_index),
        )
        self.assertTrue(
            popen.call_args.kwargs["env"]["PATH"].startswith(str(root / "tools"))
        )
        roles = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--voice-candidate-role"
        ]
        self.assertEqual(roles, ["Rhiannon"])

    def test_voice_candidate_preparation_blocks_concurrent_prune(self):
        from vntts.voice_candidate_cache import prune_obsolete_voice_candidate_caches

        with TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            output = base / "imports"
            root = output / "reverse1999" / "voice-candidates"
            candidate = root / ("a" * 24)
            candidate.mkdir(parents=True)
            manifest = candidate / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            jobs = base / "jobs"
            jobs.mkdir()
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",), output_root=output
            )

            def prepare(*_args, **_kwargs):
                self.assertEqual(prune_obsolete_voice_candidate_caches(root, jobs), ())
                self.assertTrue(candidate.exists())
                return json.dumps({"voice_manifest": str(manifest)}), ""

            with (
                patch(
                    "vntts.game_content_importer.ensure_game_decoder",
                    return_value=base / "vgmstream-cli",
                ),
                patch.object(importer, "_run", side_effect=prepare),
                patch("vntts.voice_candidate_cache._claim_candidate"),
            ):
                self.assertEqual(
                    importer.prepare_voice_roles(("Rhiannon",)), manifest.resolve()
                )
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(root, jobs),
                (candidate.resolve(),),
            )

    def test_voice_candidate_manifest_must_not_be_a_symlink(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate_root = root / "imports/reverse1999/voice-candidates/id"
            candidate_root.mkdir(parents=True)
            actual = candidate_root / "actual.json"
            actual.write_text("{}", encoding="utf-8")
            alias = candidate_root / "manifest.json"
            symlink_or_skip(alias, actual)
            importer = Reverse1999GameImporter(
                command=("r1999-bootstrap",),
                output_root=root / "imports",
            )

            with (
                patch(
                    "vntts.game_content_importer.ensure_game_decoder",
                    return_value=root / "vgmstream-cli",
                ),
                patch.object(
                    importer,
                    "_run",
                    return_value=(
                        json.dumps({"voice_manifest": str(alias)}),
                        "",
                    ),
                ),
                self.assertRaisesRegex(GameContentImportError, "usable manifest"),
            ):
                importer.prepare_voice_roles(("Rhiannon",))

    def test_prepares_selected_role_from_playable_or_other_story_catalog(self):
        for source_line in ("playable-voice:1:2:0", "other-story:rhiannon:1"):
            with (
                self.subTest(source_line=source_line),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                story = write_content(root / "content")
                metadata, *records = [
                    json.loads(line) for line in story.read_text().splitlines()
                ]
                original = next(
                    row for row in records if row.get("line_id") == "line:original"
                )
                write_story_index_document(
                    story, metadata, [row for row in records if row is not original]
                )
                content = inspect_story_index(story, provider_id="reverse1999")
                job = PregenerationJobStore(root / "jobs").create_or_resume(
                    content, ("story",)
                )
                importer = Reverse1999GameImporter(output_root=root / "imports")
                with patch.object(
                    importer, "prepare_voice_roles", return_value=root / "manifest.json"
                ) as prepare:
                    self.assertIsNone(importer.prepare_voice_candidates(job))
                    prepare.assert_not_called()
                    catalog = (
                        importer.output_root / "reverse1999" / "narrator-index.jsonl"
                    )
                    catalog.parent.mkdir(parents=True)
                    original["line_id"] = source_line
                    original["chapter"] = "outside-selected-story"
                    if source_line.startswith("playable-voice:"):
                        original["source_bank"] = "hero1_battle.bnk"
                        write_story_index_document(catalog, metadata, records)
                        self.assertIsNone(importer.prepare_voice_candidates(job))
                        prepare.assert_not_called()
                        original["source_bank"] = "hero1_mainvoc.bnk"
                    write_story_index_document(catalog, metadata, records)
                    self.assertEqual(
                        importer.prepare_voice_candidates(job), root / "manifest.json"
                    )
                    prepare.assert_called_once_with(
                        ("Rhiannon",),
                        None,
                        progress=None,
                        target_story_index=job.story_index,
                    )

                    cache = catalog.parent / "playable-voice-roles.json"
                    saved_roles = json.loads(cache.read_text(encoding="utf-8"))
                    saved_roles["roles"].append("")
                    cache.write_text(json.dumps(saved_roles), encoding="utf-8")

                    with patch(
                        "vntts.game_content_importer.load_story_index_document",
                        side_effect=AssertionError("reference index was reparsed"),
                    ):
                        self.assertEqual(
                            importer.prepare_voice_candidates(job),
                            root / "manifest.json",
                        )

                    catalog.write_text("broken catalog", encoding="utf-8")
                    with self.assertRaises(GameContentImportError):
                        importer.prepare_voice_candidates(job)


if __name__ == "__main__":
    unittest.main()
