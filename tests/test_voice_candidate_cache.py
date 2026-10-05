import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts import voice_candidate_cache as cache
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

    def test_root_relative_protected_manifest_is_not_pruned(self) -> None:
        old = self._candidate("old")
        current = self._candidate("current")
        manifest = current.relative_to(self.root) / "manifest.json"

        removed = prune_obsolete_voice_candidate_caches(
            self.root, self.jobs, protected_paths=(manifest,)
        )

        self.assertEqual(removed, (old.resolve(),))
        self.assertTrue(current.exists())

    def test_removes_a_bounded_batch_when_cache_exceeds_previous_limit(self) -> None:
        stale = tuple(self._candidate(f"stale-{index}") for index in range(65))

        removed = prune_obsolete_voice_candidate_caches(self.root, self.jobs)

        self.assertEqual(len(removed), 8)
        self.assertTrue(all(not directory.exists() for directory in removed))
        self.assertEqual(sum(directory.exists() for directory in stale), 57)

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

    def test_current_process_claim_keeps_candidate_until_release(self) -> None:
        claimed = self._candidate("claimed")
        with (
            ExitStack() as claims,
            patch.object(cache, "_candidate_claims", claims),
            patch.object(cache, "_claimed_paths", set()),
        ):
            cache.claim_voice_candidate_cache(self.root, claimed / "manifest.json")
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
            )
            self.assertTrue(claimed.exists())
        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs),
            (claimed.resolve(),),
        )

    def test_another_process_claim_keeps_candidate_until_exit(self) -> None:
        claimed = self._candidate("claimed")
        obsolete = self._candidate("obsolete")
        ready = Path(self.temporary.name) / "claimed-ready"
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "from vntts.voice_candidate_cache import claim_voice_candidate_cache\n"
            "root, manifest, ready = map(Path, sys.argv[1:])\n"
            "claim_voice_candidate_cache(root, manifest)\n"
            "ready.write_text('ready')\n"
            "sys.stdin.read()\n"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.root),
                str(claimed / "manifest.json"),
                str(ready),
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(
                ready.exists(),
                process.stderr.read().decode()
                if process.poll() is not None
                else "claim timed out",
            )
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs),
                (obsolete.resolve(),),
            )
            self.assertTrue(claimed.exists())
        finally:
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=5)
            assert process.stderr is not None
            process.stderr.close()
        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs),
            (claimed.resolve(),),
        )

    def test_malformed_saved_reference_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        (job / "voice-plan.json").write_text("{", encoding="utf-8")

        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
        )
        self.assertTrue(old.exists())

    def test_growing_reference_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        reference = job / "voice-plan.json"
        reference.write_text("{}", encoding="utf-8")
        original_open = Path.open

        def grow_before_read(path, mode="r", *args, **kwargs):
            if path == reference and mode in {"r", "rb"}:
                with original_open(reference, "wb") as destination:
                    destination.write(b"{}" + b" " * 256)
            return original_open(path, mode, *args, **kwargs)

        with (
            patch.object(cache, "_MAX_REFERENCE_DOCUMENT_BYTES", 128),
            patch.object(Path, "open", autospec=True, side_effect=grow_before_read),
        ):
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
            )
        self.assertTrue(old.exists())

    def test_deep_reference_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        (job / "voice-plan.json").write_text(
            "[" * 2000 + "0" + "]" * 2000, encoding="utf-8"
        )

        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
        )
        self.assertTrue(old.exists())

    def test_unresolvable_home_path_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        reference = job / "voice-plan.json"
        unknown = Path("~vntts-nonexistent-user/reference.wav")
        original_expanduser = Path.expanduser

        def expand_known_user(path):
            if path == unknown:
                raise RuntimeError("Could not determine home directory")
            return original_expanduser(path)

        for location in ("candidate_root", "job_root", "protected_path", "reference"):
            with self.subTest(location=location):
                reference.write_text(
                    json.dumps(
                        {"note": str(unknown) if location == "reference" else ""}
                    ),
                    encoding="utf-8",
                )
                with patch.object(
                    Path, "expanduser", autospec=True, side_effect=expand_known_user
                ):
                    self.assertEqual(
                        prune_obsolete_voice_candidate_caches(
                            unknown if location == "candidate_root" else self.root,
                            unknown if location == "job_root" else self.jobs,
                            protected_paths=(unknown,)
                            if location == "protected_path"
                            else (),
                        ),
                        (),
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

    def test_unreadable_candidate_subtree_defers_all_cleanup(self) -> None:
        old = self._candidate("old")
        unreadable = self._candidate("unreadable")
        child = unreadable / "nested"
        child.mkdir()
        (child / "reference.wav").write_bytes(b"audio")
        original_scandir = cache.os.scandir
        canonical_child = child.resolve()
        denied = []

        def deny_subtree(path):
            if not isinstance(path, int) and Path(path) == canonical_child:
                denied.append(canonical_child)
                raise PermissionError("candidate subtree cannot be inspected")
            return original_scandir(path)

        with patch.object(cache.os, "scandir", side_effect=deny_subtree):
            removed = prune_obsolete_voice_candidate_caches(self.root, self.jobs)
        self.assertEqual(denied, [canonical_child])
        self.assertEqual(removed, ())
        self.assertTrue(old.exists())
        self.assertEqual((child / "reference.wav").read_bytes(), b"audio")

    def test_dangling_saved_reference_alias_defers_cleanup(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        pack = job / "game-packs" / ("pack-" + "b" * 24)
        for location in ("voice-plan.json", "game-packs", "game-pack.json"):
            with self.subTest(location=location):
                if not old.exists():
                    old = self._candidate("old")
                alias = (
                    pack / location if location == "game-pack.json" else job / location
                )
                alias.parent.mkdir(parents=True, exist_ok=True)
                symlink_or_skip(
                    alias,
                    job / "absent-reference",
                    target_is_directory=location == "game-packs",
                )
                try:
                    self.assertEqual(
                        prune_obsolete_voice_candidate_caches(self.root, self.jobs),
                        (),
                    )
                    self.assertTrue(old.exists())
                finally:
                    alias.unlink()

    def _candidate(self, name: str) -> Path:
        directory = self.root / hashlib.sha256(name.encode()).hexdigest()[:24]
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        return directory
