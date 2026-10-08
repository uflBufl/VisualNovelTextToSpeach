import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts import load_story_index_document

import vntts.game_content_importer as importer
import vntts.game_pack as game_pack
import vntts.pregeneration_setup as setup
import vntts.story_index_snapshot as snapshots
from tests.story_fixtures import write_story_index


class StoryIndexSnapshotTest(unittest.TestCase):
    def setUp(self):
        setup._cached_story_index_document.cache_clear()
        self.addCleanup(setup._cached_story_index_document.cache_clear)

    def test_cache_data_stays_bound_through_transient_source_replacement(self):
        for owner in (setup, importer, game_pack):
            error_type = {
                setup: setup.StoryContentChanged,
                importer: importer.GameContentImportError,
                game_pack: game_pack.GamePackError,
            }[owner]
            for restore in (True, False):
                with (
                    self.subTest(owner=owner.__name__, restore=restore),
                    TemporaryDirectory() as directory,
                ):
                    path = write_story_index(Path(directory))
                    original = path.read_bytes()
                    checksum = hashlib.sha256(original).hexdigest()
                    expected = load_story_index_document(path)
                    rows = [json.loads(row) for row in original.decode().splitlines()]
                    rows[1]["speaker"] = "Unbound role"
                    rows[1]["voice_character"] = "Unbound role"
                    replacement = (
                        "\n".join(json.dumps(row) for row in rows) + "\n"
                    ).encode()
                    cache = path.with_name("playable-voice-roles.json")

                    def load():
                        if owner is setup:
                            return setup.load_verified_story_index_document(
                                path, checksum
                            )
                        if owner is game_pack:
                            return game_pack._load_bound_story_index(path, checksum)
                        return importer._cached_playable_voice_roles(path)

                    def replace_during_parse(source):
                        path.write_bytes(replacement)
                        try:
                            return load_story_index_document(source)
                        finally:
                            if restore:
                                path.write_bytes(original)

                    with (
                        patch.object(
                            owner,
                            "load_story_index_document",
                            side_effect=replace_during_parse,
                            create=True,
                        ),
                        patch.object(
                            snapshots,
                            "load_story_index_document",
                            side_effect=replace_during_parse,
                        ) as parse,
                    ):
                        if not restore:
                            with self.assertRaisesRegex(error_type, "changed while"):
                                load()
                            self.assertFalse(cache.exists())
                            self.assertEqual(
                                setup._cached_story_index_document.cache_info().currsize,
                                0,
                            )
                            continue
                        actual = load()
                        self.assertEqual(
                            actual, {"centurion"} if owner is importer else expected
                        )
                        self.assertEqual(parse.call_count, 1)
                        self.assertEqual(load(), actual)
                        self.assertEqual(
                            parse.call_count, 2 if owner is game_pack else 1
                        )
                    if owner is importer:
                        self.assertEqual(
                            json.loads(cache.read_text())["index_sha256"], checksum
                        )
                    self.assertEqual(path.read_bytes(), original)
                    setup._cached_story_index_document.cache_clear()

    def test_changed_captured_bytes_reject_before_parsing_or_cache_publication(self):
        for owner in (setup, importer, game_pack):
            error_type = {
                setup: setup.StoryContentChanged,
                importer: importer.GameContentImportError,
                game_pack: game_pack.GamePackError,
            }[owner]
            with self.subTest(owner=owner.__name__), TemporaryDirectory() as directory:
                path = write_story_index(Path(directory))
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                path.write_bytes(path.read_bytes() + b"\n")
                with (
                    patch.object(owner, "sha256_file", return_value=checksum),
                    patch.object(snapshots, "load_story_index_document") as parse,
                    self.assertRaisesRegex(error_type, "changed while"),
                ):
                    if owner is setup:
                        setup.load_verified_story_index_document(path, checksum)
                    elif owner is game_pack:
                        game_pack._load_bound_story_index(path, checksum)
                    else:
                        importer._cached_playable_voice_roles(path)
                parse.assert_not_called()
                self.assertFalse(path.with_name("playable-voice-roles.json").exists())
                self.assertEqual(
                    setup._cached_story_index_document.cache_info().currsize, 0
                )


if __name__ == "__main__":
    unittest.main()
