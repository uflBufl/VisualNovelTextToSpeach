import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.pregeneration_fixtures import voice_impact_fixture
from vntts.pregeneration_setup import (
    load_verified_story_index_document,
)
from vntts.pregeneration_voices import PregenerationVoiceError
from vntts.voice_default_impact import inspect_voice_default_impact


class VoiceDefaultImpactTest(unittest.TestCase):
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
