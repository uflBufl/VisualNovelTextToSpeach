import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.workbench_contracts import (
    AuthoringWorkbenchError,
    _OutcomeMergeSources,
)
from vntts.authoring.workspace_outcome_merge import _overlay_outcome_merge_items


class WorkspaceAudioPathOwnershipTest(unittest.TestCase):
    def test_new_outcomes_cannot_overwrite_each_other(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            relative = Path("shared.wav")
            sources = _OutcomeMergeSources(
                items={
                    queue_id: ({"path": str(relative)}, {"queue_id": queue_id})
                    for queue_id in ("first", "second")
                },
                records=[],
                snapshots=[],
                audio={
                    queue_id: (output / f"{queue_id}.wav", queue_id.encode(), relative)
                    for queue_id in ("first", "second")
                },
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "collides with 'first'"
            ):
                _overlay_outcome_merge_items(output, {"items": {}}, {}, sources)
            self.assertEqual((output / relative).read_bytes(), b"first")


if __name__ == "__main__":
    unittest.main()
