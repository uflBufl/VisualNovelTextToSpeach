import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from tests.bulk_generation_fixtures import additive_queue_item as item
from vntts.authoring import queue_extension
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.queue_extension import (
    FIELD,
    QueueExtensionError,
    publish_additive_generation_queue,
    validate_additive_generation_queue,
    workspace_queue_extension,
)


class QueueExtensionTest(unittest.TestCase):
    def test_workspace_binding_matches_validated_queue_and_preserves_public_contract(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [item(1)]
            )
            addition = write_voice_generation_queue(
                root / "addition.jsonl", metadata, [item(2)]
            )
            output = publish_additive_generation_queue(
                base, addition, root / "combined.jsonl"
            )
            queue, ledger = validate_additive_generation_queue(output, base_queue=base)
            self.assertEqual(queue.path, output)
            self.assertIs(ledger, queue.metadata[FIELD])
            binding = workspace_queue_extension(output, base_queue=base)
            self.assertEqual(binding["queue_sha256"], sha256_file(output))
            self.assertEqual(binding["extension_id"], ledger["extension_id"])
            self.assertEqual(binding["added_queue_ids"], [item(2)["queue_id"]])

    def test_workspace_binding_rejects_target_replacement_after_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [item(1)]
            )
            addition = write_voice_generation_queue(
                root / "addition.jsonl", metadata, [item(2)]
            )
            alternate_addition = write_voice_generation_queue(
                root / "alternate-addition.jsonl", metadata, [item(3)]
            )
            output = publish_additive_generation_queue(
                base, addition, root / "combined.jsonl"
            )
            replacement = publish_additive_generation_queue(
                base, alternate_addition, root / "replacement.jsonl"
            ).read_bytes()
            sources = {path: path.read_bytes() for path in (base, addition)}
            validate = queue_extension._validate_extension_ledger

            def replace_after_validation(ledger, queue, *, base_queue):
                validated = validate(ledger, queue, base_queue=base_queue)
                output.write_bytes(replacement)
                return validated

            with (
                patch.object(
                    queue_extension,
                    "_validate_extension_ledger",
                    side_effect=replace_after_validation,
                ),
                self.assertRaisesRegex(QueueExtensionError, "target changed"),
            ):
                workspace_queue_extension(output, base_queue=base)
            self.assertEqual(output.read_bytes(), replacement)
            for path, payload in sources.items():
                self.assertEqual(path.read_bytes(), payload)

    def test_source_replacement_after_capture_is_refused_before_publication(self):
        for source_index, label in ((1, "base"), (2, "extension")):
            with self.subTest(source=label), TemporaryDirectory() as directory:
                root = Path(directory)
                metadata = {"game": "Reverse: 1999", "language": "en"}
                base = write_voice_generation_queue(
                    root / "base.jsonl", metadata, [item(1)]
                )
                extension = write_voice_generation_queue(
                    root / "extension.jsonl", metadata, [item(2)]
                )
                source = base if source_index == 1 else extension
                load = VoiceGenerationQueue.load
                loads = 0

                def replace_after_parse(path):
                    nonlocal loads
                    queue = load(path)
                    loads += 1
                    if loads == source_index:
                        write_voice_generation_queue(
                            source,
                            metadata,
                            [item(source_index, "Changed after capture.")],
                        )
                    return queue

                output = root / "combined.jsonl"
                with (
                    patch.object(
                        VoiceGenerationQueue, "load", side_effect=replace_after_parse
                    ),
                    self.assertRaisesRegex(QueueExtensionError, f"{label} changed"),
                ):
                    publish_additive_generation_queue(base, extension, output)
                self.assertFalse(output.exists())

    def test_changed_malformed_base_is_refused_before_parsing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [item(1)]
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl", metadata, [item(2)]
            )
            output = publish_additive_generation_queue(
                base, extension, root / "combined.jsonl"
            )
            before = output.read_bytes()
            base.write_bytes(b"invalid replacement")
            with self.assertRaisesRegex(QueueExtensionError, "base changed"):
                validate_additive_generation_queue(output, base_queue=base)
            self.assertEqual(output.read_bytes(), before)

    def test_missing_base_uses_domain_error_and_preserves_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [item(1)]
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl", metadata, [item(2)]
            )
            output = publish_additive_generation_queue(
                base, extension, root / "combined.jsonl"
            )
            original = output.read_bytes()
            base.unlink()
            with self.assertRaises(QueueExtensionError) as caught:
                validate_additive_generation_queue(output, base_queue=base)
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            self.assertEqual(output.read_bytes(), original)

    def test_ledger_versions_and_counts_require_real_integers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [item(1)]
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl", metadata, [item(2)]
            )
            output = publish_additive_generation_queue(
                base, extension, root / "combined.jsonl"
            )
            queue, ledger = validate_additive_generation_queue(output, base_queue=base)
            for field in ("schema_version", "base_item_count", "added_item_count"):
                for invalid in (True, 1.0, [], {}, None, "1"):
                    with self.subTest(field=field, invalid=invalid):
                        altered = {**ledger, field: invalid}
                        altered["extension_id"] = canonical_document_sha256(
                            {
                                key: value
                                for key, value in altered.items()
                                if key != "extension_id"
                            }
                        )
                        write_voice_generation_queue(
                            output,
                            {**queue.metadata, FIELD: altered},
                            [entry.document for entry in queue.items],
                        )
                        with self.assertRaises(QueueExtensionError):
                            validate_additive_generation_queue(output, base_queue=base)

    def test_publishes_strict_ordered_superset_with_bound_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base_items = [item(1), item(3)]
            extension_items = [item(2)]
            base = write_voice_generation_queue(
                root / "base.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                base_items,
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl",
                {
                    "game": "Reverse: 1999",
                    "language": "en",
                    "partial_source_audio_count": 1,
                },
                extension_items,
            )

            output = publish_additive_generation_queue(
                base, extension, root / "combined.jsonl"
            )
            queue = VoiceGenerationQueue.load(output)
            ledger = queue.metadata[FIELD]
            base_sha256 = sha256_file(base)
            extension_sha256 = sha256_file(extension)

        self.assertEqual(
            [value.document["sequence"] for value in queue.items], [1, 2, 3]
        )
        self.assertEqual(ledger["base_queue_sha256"], base_sha256)
        self.assertEqual(ledger["extension_queue_sha256"], extension_sha256)
        self.assertEqual(ledger["base_item_count"], 2)
        self.assertEqual(ledger["added_item_count"], 1)
        self.assertEqual(ledger["added_items"][0]["queue_id"], item(2)["queue_id"])

    def test_rejects_collisions_and_leaves_destination_absent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = write_voice_generation_queue(
                root / "base.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                [item(1)],
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                [item(1)],
            )
            output = root / "combined.jsonl"

            with self.assertRaisesRegex(QueueExtensionError, "collides"):
                publish_additive_generation_queue(base, extension, output)

        self.assertFalse(output.exists())

    def test_rejects_changed_game(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = write_voice_generation_queue(
                root / "base.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                [item(1)],
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl",
                {"game": "Another game", "language": "en"},
                [item(2)],
            )

            with self.assertRaisesRegex(QueueExtensionError, "game differs"):
                publish_additive_generation_queue(
                    base, extension, root / "combined.jsonl"
                )

    def test_validation_rejects_changed_base_or_added_item(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = write_voice_generation_queue(
                root / "base.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                [item(1)],
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl",
                {"game": "Reverse: 1999", "language": "en"},
                [item(2)],
            )
            output = publish_additive_generation_queue(
                base, extension, root / "combined.jsonl"
            )
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

            base_changed = [dict(record) for record in records]
            next(
                record
                for record in base_changed
                if record.get("queue_id") == item(1)["queue_id"]
            )["speaker"] = "Centurion"
            base_changed_path = root / "base-changed.jsonl"
            base_changed_path.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in base_changed
                ),
                encoding="utf-8",
            )
            with self.assertRaises(QueueExtensionError):
                validate_additive_generation_queue(base_changed_path, base_queue=base)

            added_changed = [dict(record) for record in records]
            next(
                record
                for record in added_changed
                if record.get("queue_id") == item(2)["queue_id"]
            )["speaker"] = "Centurion"
            added_changed_path = root / "added-changed.jsonl"
            added_changed_path.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in added_changed
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(QueueExtensionError, "added item changed"):
                validate_additive_generation_queue(added_changed_path, base_queue=base)


if __name__ == "__main__":
    unittest.main()
