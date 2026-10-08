import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from tests.authoring_fixtures import (
    queue_builder_story_record,
    tree_hashes,
    write_queue_builder_inputs,
)
from tests.bulk_generation_fixtures import additive_queue_item
from vntts.authoring.cli import main
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.authoring.queue_extension import (
    QueueExtensionError,
    publish_additive_generation_queue,
)


class QueuePublicationErrorTest(unittest.TestCase):
    def test_publication_errors_preserve_sources_and_existing_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = write_queue_builder_inputs(
                root / "inputs", [queue_builder_story_record("line-1", "absent")]
            )
            plan = inspect_generation_queue(story, manifest)
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [additive_queue_item(1)]
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl", metadata, [additive_queue_item(2)]
            )
            output = root / "blocked"
            output.mkdir()
            (output / "keep.txt").write_text("Keep existing work.", encoding="utf-8")
            original = tree_hashes(root)
            for error_type, publish in (
                (
                    GenerationQueueBuildError,
                    lambda: publish_generation_queue(plan, output),
                ),
                (
                    QueueExtensionError,
                    lambda: publish_additive_generation_queue(base, extension, output),
                ),
            ):
                with self.subTest(error=error_type.__name__):
                    with self.assertRaises(error_type) as caught:
                        publish()
                    self.assertIsInstance(caught.exception.__cause__, IsADirectoryError)
                    self.assertEqual(tree_hashes(root), original)
            for arguments in (
                [
                    "build-queue",
                    "--story-index",
                    str(story),
                    "--voice-manifest",
                    str(manifest),
                    "--output",
                    str(output),
                ],
                ["extend-queue", str(base), str(extension), "--output", str(output)],
            ):
                with self.subTest(command=arguments[0]):
                    error = io.StringIO()
                    with (
                        redirect_stderr(error),
                        self.assertRaises(SystemExit) as caught,
                    ):
                        main(arguments)
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn(str(output), error.getvalue())
                    self.assertNotIn("Traceback", error.getvalue())
                    self.assertEqual(tree_hashes(root), original)
