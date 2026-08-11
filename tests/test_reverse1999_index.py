import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.reverse1999_index import (
    build_bank_index,
    classify_bank,
    main,
)
from vntts.wwise import WwiseBankError, WwiseBankSummary


class Reverse1999BankIndexTest(unittest.TestCase):
    def test_classifies_story_and_activity_npc_bank_names(self):
        self.assertEqual(
            classify_bank("plotvoc_npc522301chapter9.bnk"),
            ("story-npc", ["npc", "story", "voice"]),
        )
        self.assertEqual(
            classify_bank("activitystory_event_npc520301_voc.bnk"),
            ("activity-npc", ["npc", "story", "activity", "voice"]),
        )
        self.assertEqual(
            classify_bank("plotvoc_npcnoname218chapter11.bnk"),
            ("story-npc", ["npc", "story", "voice"]),
        )

    def test_builds_searchable_npc_index_and_records_bank_metadata(self):
        summary = WwiseBankSummary(
            bank_version=154,
            sections=("BKHD", "DIDX", "DATA", "HIRC"),
            media_ids=(10, 20),
            embedded_media_bytes=1200,
            hirc_object_count=4,
        )
        inspector = Mock(return_value=summary)
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank = root / "plotvoc_npc522301chapter9.bnk"
            bank.write_bytes(b"bank")
            output = root / "index.json"

            index, result = build_bank_index(
                root,
                output=output,
                inspector=inspector,
            )
            saved = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, output.resolve())
        self.assertEqual(index["bank_count"], 1)
        self.assertEqual(index["npc_banks"], {"522301": [bank.name]})
        self.assertEqual(index["categories"], {"story-npc": 1})
        self.assertEqual(index["banks"][0]["chapters"], [9])
        self.assertEqual(index["banks"][0]["media_count"], 2)
        self.assertEqual(saved["banks"][0]["hirc_object_count"], 4)
        inspector.assert_called_once_with(bank.resolve())

    def test_reuses_unchanged_entries_and_reinspects_changed_banks(self):
        summary = WwiseBankSummary(154, ("BKHD",), (), 0, None)
        first_inspector = Mock(return_value=summary)
        second_inspector = Mock(return_value=summary)
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank = root / "npc513702_level01.bnk"
            bank.write_bytes(b"first")
            output = root / "index.json"
            build_bank_index(root, output=output, inspector=first_inspector)

            reused, _output = build_bank_index(
                root,
                output=output,
                inspector=second_inspector,
            )
            bank.write_bytes(b"changed-bank")
            changed, _output = build_bank_index(
                root,
                output=output,
                inspector=second_inspector,
            )

        self.assertEqual(reused["reused_count"], 1)
        self.assertEqual(changed["reused_count"], 0)
        first_inspector.assert_called_once_with(bank.resolve())
        second_inspector.assert_called_once_with(bank.resolve())

    def test_records_a_broken_bank_without_aborting_the_scan(self):
        inspector = Mock(side_effect=WwiseBankError("broken HIRC"))
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "bad.bnk").write_bytes(b"bad")

            index, _output = build_bank_index(
                root,
                output=root / "index.json",
                inspector=inspector,
            )

        self.assertEqual(index["error_count"], 1)
        self.assertEqual(index["banks"][0]["error"], "broken HIRC")

    def test_cli_uses_discovered_game_directory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "index.json"
            result = ({"bank_count": 2, "reused_count": 1, "error_count": 0}, output)
            with (
                patch(
                    "vntts.reverse1999_index.find_game_audio_directory",
                    return_value=root,
                ),
                patch(
                    "vntts.reverse1999_index.build_bank_index",
                    return_value=result,
                ) as build,
            ):
                status = main(["--output", str(output)])

        self.assertEqual(status, 0)
        self.assertEqual(build.call_args.args[0], root)


if __name__ == "__main__":
    unittest.main()
