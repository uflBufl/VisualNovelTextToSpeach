import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from vntts.authoring.queue_extension import (
    FIELD,
    QueueExtensionError,
    publish_additive_generation_queue,
    validate_additive_generation_queue,
)


def item(sequence, text=None):
    text = text or f"Line {sequence}."
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"line:{sequence}:{digest[:16]}",
        "line_id": f"line:{sequence}",
        "text_sha256": digest,
        "text": text,
        "speaker": "Rhiannon",
        "voice_character": "Rhiannon",
        "action": "generate",
        "sequence": sequence,
        "story_order": 1000 + sequence,
    }


class QueueExtensionTest(unittest.TestCase):
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
