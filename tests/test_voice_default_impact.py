import os
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.voice_default_impact as impact_module
import vntts.voice_library as voice_library
from tests.authoring_fixtures import tree_hashes
from tests.pregeneration_fixtures import voice_impact_fixture
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.pregeneration_setup import (
    load_verified_story_index_document,
)
from vntts.pregeneration_voices import PregenerationVoiceError
from vntts.voice_default_impact import inspect_voice_default_impact


class VoiceDefaultImpactTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_second_library_copy_refusal_cleans_preview_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            content, jobs, decisions, settings, _pack, library = voice_impact_fixture(
                root
            )
            proposed = library.copy_to(root / "proposed-library")
            proposed.select("Rhiannon", route="voice", source_id="preset:marius")
            inputs = dict(
                current_voice_library=library,
                proposed_voice_library=proposed,
            )
            expected = inspect_voice_default_impact(
                content, jobs, decisions, settings, "Rhiannon", **inputs
            )
            before = tree_hashes(root)
            original_copy = voice_library.copy_regular_file
            original_temporary = impact_module.temporary_directory
            directories = []
            refused = []

            @contextmanager
            def temporary(**kwargs):
                with original_temporary(dir=root, **kwargs) as name:
                    directories.append(Path(name))
                    yield name

            def copy(original, target, **kwargs):
                if Path(original) != proposed.path:
                    return original_copy(original, target, **kwargs)
                self.assertTrue(
                    (directories[-1] / "current-voices" / library.path.name).is_file()
                )
                gate: RegularFileGate = {"path": proposed.path, "armed": True}
                with (
                    swap_at_acquisition(self, gate),
                    self.assertRaisesRegex(OSError, "regular file") as caught,
                ):
                    original_copy(original, target, **kwargs)
                self.assertFalse(Path(target).exists())
                refused.append((Path(target), caught.exception))
                raise caught.exception

            with patch.object(impact_module, "temporary_directory", temporary):
                with (
                    patch.object(voice_library, "copy_regular_file", copy),
                    self.assertRaises(shutil.Error) as caught,
                ):
                    inspect_voice_default_impact(
                        content, jobs, decisions, settings, "Rhiannon", **inputs
                    )
                self.assertEqual(len(refused), 1)
                target, error = refused[0]
                self.assertEqual(
                    caught.exception.args[0],
                    [(str(proposed.path), str(target), str(error))],
                )
                self.assertFalse(directories[-1].exists())
                self.assertEqual(tree_hashes(root), before)
                recovered = inspect_voice_default_impact(
                    content, jobs, decisions, settings, "Rhiannon", **inputs
                )
            self.assertEqual(recovered, expected)
            self.assertEqual(len(directories), 2)
            self.assertTrue(all(not temporary.exists() for temporary in directories))
            self.assertEqual(tree_hashes(root), before)

    def test_changed_default_compares_saved_voices_and_keeps_original_and_unrelated_stories(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content, jobs, decisions, settings, pack, library = voice_impact_fixture(
                root
            )
            proposed = library.copy_to(root / "proposed-library")
            proposed.select("Rhiannon", route="voice", source_id="preset:marius")
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            with patch(
                "vntts.voice_default_impact.load_verified_story_index_document",
                wraps=load_verified_story_index_document,
            ) as parse:
                result = inspect_voice_default_impact(
                    content,
                    jobs,
                    decisions,
                    settings,
                    "Rhiannon",
                    current_voice_library=library,
                    proposed_voice_library=proposed,
                )
            self.assertEqual(
                [Path(call.args[0]).resolve() for call in parse.call_args_list],
                [
                    content.story_index.resolve(),
                    (pack / "story-index.jsonl").resolve(),
                ],
            )
            self.assertEqual(result[0].changed_line_ids, ("changed",))
            self.assertEqual(
                (result[0].matching, result[0].unknown, result[0].original), (1, 1, 0)
            )
            self.assertEqual(result[1].changed_line_ids, ())
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
            )

    def test_narrator_change_includes_fallback_and_unknown_roles_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content, jobs, decisions, settings, _pack, library = voice_impact_fixture(
                root
            )
            proposed = library.copy_to(root / "proposed-library")
            proposed.select("Narrator", route="voice", source_id="preset:marius")
            result = inspect_voice_default_impact(
                content,
                jobs,
                decisions,
                settings,
                "Narrator",
                current_voice_library=library,
                proposed_voice_library=proposed,
            )
            self.assertEqual(result[0].changed_line_ids, ())
            self.assertEqual(
                result[1].changed_line_ids, ("fallback", "unknown-speaker")
            )

    def test_linked_voice_change_includes_all_names_and_keeps_saved_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content, jobs, decisions, settings, _pack, library = voice_impact_fixture(
                root
            )
            library.link_person("Rhiannon", "Centurion")
            proposed = library.copy_to(root / "proposed-library")
            proposed.select("Centurion", route="voice", source_id="preset:marius")
            before = tree_hashes(root)
            for role in ("Rhiannon", "Centurion"):
                with self.subTest(role=role):
                    result = inspect_voice_default_impact(
                        content,
                        jobs,
                        decisions,
                        settings,
                        role,
                        current_voice_library=library,
                        proposed_voice_library=proposed,
                    )
                    self.assertEqual(result[0].changed_line_ids, ("changed",))
                    self.assertEqual(result[1].changed_line_ids, ("other",))
            self.assertEqual(tree_hashes(root), before)

    def test_link_changes_compare_the_old_and_new_canonical_families(self):
        for linked_in in ("current", "proposed"):
            with self.subTest(linked_in=linked_in), TemporaryDirectory() as directory:
                root = Path(directory)
                content, jobs, decisions, settings, _pack, library = (
                    voice_impact_fixture(root)
                )
                proposed = library.copy_to(root / "proposed-library")
                linked = library if linked_in == "current" else proposed
                linked.link_person("Rhiannon", "Centurion")
                proposed.select("Rhiannon", route="voice", source_id="preset:marius")
                result = inspect_voice_default_impact(
                    content,
                    jobs,
                    decisions,
                    settings,
                    "Centurion",
                    current_voice_library=library,
                    proposed_voice_library=proposed,
                )
                self.assertEqual(result[0].changed_line_ids, ("changed",))
                self.assertEqual(
                    result[1].changed_line_ids,
                    ("other",) if linked_in == "proposed" else (),
                )

    def test_damaged_recording_cannot_be_reported_as_a_reusable_voice(self):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, pack, library = voice_impact_fixture(
                Path(directory)
            )
            (pack / "changed.wav").write_bytes(b"damaged")
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "(checksum|missing|damaged|integrity)"
            ):
                inspect_voice_default_impact(
                    content,
                    jobs,
                    decisions,
                    settings,
                    "Rhiannon",
                    current_voice_library=library,
                    proposed_voice_library=library,
                )

    def test_changed_source_requires_refresh_before_comparison(self):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, _pack, library = voice_impact_fixture(
                Path(directory)
            )
            content.story_index.write_text(content.story_index.read_text() + "\n")
            with self.assertRaisesRegex(
                PregenerationVoiceError, "Story content changed"
            ):
                inspect_voice_default_impact(
                    content,
                    jobs,
                    decisions,
                    settings,
                    "Rhiannon",
                    current_voice_library=library,
                    proposed_voice_library=library,
                )
