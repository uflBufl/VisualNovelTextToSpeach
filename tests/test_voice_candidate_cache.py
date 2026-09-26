import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.symlink_support import symlink_or_skip
from vntts.voice_candidate_cache import prune_obsolete_voice_candidate_caches


class VoiceCandidateCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "voice-candidates"
        self.jobs = Path(self.temporary.name) / "jobs"
        self.root.mkdir()
        self.jobs.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_removes_unreferenced_cache_but_keeps_current_manifest(self) -> None:
        old = self._candidate("old")
        current = self._candidate("current")

        removed = prune_obsolete_voice_candidate_caches(
            self.root, self.jobs, protected_paths=(current / "manifest.json",)
        )

        self.assertEqual(removed, (old.resolve(),))
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())

    def test_keeps_manifest_referenced_by_saved_voice_plan(self) -> None:
        old = self._candidate("old")
        saved = self._candidate("saved")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        (job / "voice-plan.json").write_text(
            json.dumps({"voice_manifest": str(saved / "manifest.json")}),
            encoding="utf-8",
        )

        removed = prune_obsolete_voice_candidate_caches(self.root, self.jobs)

        self.assertEqual(removed, (old.resolve(),))
        self.assertTrue(saved.exists())

    def test_keeps_manifest_referenced_by_published_pack(self) -> None:
        old = self._candidate("old")
        published = self._candidate("published")
        pack = self.jobs / ("a" * 24) / "game-packs" / ("pack-" + "b" * 24)
        pack.mkdir(parents=True)
        (pack / "game-pack.json").write_text(
            json.dumps({"source_manifest": str(published / "manifest.json")}),
            encoding="utf-8",
        )

        removed = prune_obsolete_voice_candidate_caches(self.root, self.jobs)

        self.assertEqual(removed, (old.resolve(),))
        self.assertTrue(published.exists())

    def test_malformed_saved_reference_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        (job / "voice-plan.json").write_text("{", encoding="utf-8")

        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
        )
        self.assertTrue(old.exists())

    def test_symlink_defers_cleanup(self) -> None:
        old = self._candidate("old")
        linked = self.root / "linked"
        symlink_or_skip(linked, old, target_is_directory=True)

        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
        )
        self.assertTrue(old.exists())

    def _candidate(self, name: str) -> Path:
        directory = self.root / hashlib.sha256(name.encode()).hexdigest()[:24]
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        return directory
