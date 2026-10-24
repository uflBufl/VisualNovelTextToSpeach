import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
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

    def test_sqlite_plan_protects_references_and_corrupt_catalog_blocks_cleanup(self):
        from tests.test_sqlite_generation_integration import prepared_input
        from vntts.pregeneration_voices import voice_plan_schema_version
        from vntts.preparation_catalog import PreparationCatalog

        _prepared, plan, _job = prepared_input(Path(self.temporary.name))
        kept = self._candidate("kept").resolve()
        obsolete = self._candidate("obsolete").resolve()
        catalog = PreparationCatalog(self.jobs)
        catalog.save_plan(
            {
                **plan.to_document(),
                "voice_manifest": str(kept / "manifest.json"),
                "voice_manifest_sha256": hashlib.sha256(
                    (kept / "manifest.json").read_bytes()
                ).hexdigest(),
            },
            voice_plan_schema_version,
        )
        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs), (obsolete,)
        )
        catalog.path.write_bytes(b"damaged database")
        unused = self._candidate("unused")
        self.assertEqual(prune_obsolete_voice_candidate_caches(self.root, self.jobs), ())
        self.assertTrue(kept.is_dir())
        self.assertTrue(unused.is_dir())

    def test_directory_replacement_during_inspection_defers_all_deletions(self) -> None:
        for phase in ("tree", "references"):
            with self.subTest(phase=phase):
                old = self._candidate("old").resolve()
                changed = self._candidate("changed").resolve()
                cache.os.utime(old, ns=(1, 1))
                cache.os.utime(changed, ns=(2, 2))
                original = self.root.parent / "inspected-original"
                foreign = b"uninspected replacement"
                native_tree = cache._safe_candidate_tree
                native_references = cache._candidate_references

                def replace():
                    changed.rename(original)
                    changed.mkdir()
                    (changed / "manifest.json").write_bytes(b"{}")
                    (changed / "foreign.wav").write_bytes(foreign)

                def inspect_tree(directory):
                    result = native_tree(directory)
                    if directory == changed and phase == "tree":
                        replace()
                    return result

                def inspect_references(*args, **kwargs):
                    result = native_references(*args, **kwargs)
                    if phase == "references":
                        replace()
                    return result

                with (
                    patch.object(
                        cache, "_safe_candidate_tree", side_effect=inspect_tree
                    ),
                    patch.object(
                        cache, "_candidate_references", side_effect=inspect_references
                    ),
                    patch.object(cache.shutil, "rmtree") as delete,
                ):
                    self.assertEqual(
                        prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
                    )
                delete.assert_not_called()
                self.assertTrue(old.exists())
                self.assertEqual((changed / "foreign.wav").read_bytes(), foreign)
                self.assertEqual((original / "manifest.json").read_bytes(), b"{}")
                changed.rename(self.root.parent / f"foreign-{phase}")
                original.rename(changed)
                self.assertEqual(
                    prune_obsolete_voice_candidate_caches(self.root, self.jobs),
                    (old, changed),
                )

    def test_changed_identity_before_deletion_preserves_directory_and_recovers(
        self,
    ) -> None:
        for change in ("replacement", "metadata", "alias"):
            with self.subTest(change=change):
                directory = self._candidate("old").resolve()
                native_remove = cache._remove_candidate
                original = self.root.parent / "inspected-original"
                observed = []

                def replace_before_remove(candidate, *args):
                    observed.append(candidate)
                    if change == "metadata":
                        status = directory.stat()
                        (directory / "foreign.wav").write_bytes(b"new content")
                        cache.os.utime(
                            directory,
                            ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000),
                        )
                    else:
                        directory.rename(original)
                        if change == "alias":
                            symlink_or_skip(
                                directory, original, target_is_directory=True
                            )
                        else:
                            directory.mkdir()
                            (directory / "manifest.json").write_bytes(b"{}")
                            (directory / "foreign.wav").write_bytes(b"new content")
                    return native_remove(candidate, *args)

                with (
                    patch.object(
                        cache, "_remove_candidate", side_effect=replace_before_remove
                    ),
                    patch.object(cache.shutil, "rmtree") as delete,
                ):
                    self.assertEqual(
                        prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
                    )
                self.assertEqual(observed, [directory])
                delete.assert_not_called()
                self.assertTrue(directory.exists())
                if change == "metadata":
                    self.assertEqual(
                        (directory / "foreign.wav").read_bytes(), b"new content"
                    )
                else:
                    self.assertEqual((original / "manifest.json").read_bytes(), b"{}")
                    if change == "alias":
                        directory.unlink()
                    else:
                        self.assertEqual(
                            (directory / "foreign.wav").read_bytes(), b"new content"
                        )
                        directory.rename(self.root.parent / "foreign-replacement")
                    original.rename(directory)
                self.assertEqual(
                    prune_obsolete_voice_candidate_caches(self.root, self.jobs),
                    (directory,),
                )

    def test_oversized_directory_scan_stops_and_closes_without_deleting(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        packs = job / "game-packs"
        packs.mkdir(parents=True)
        original_scandir = cache.os.scandir
        for target, limit_name in (
            (self.jobs, "_MAX_JOBS"),
            (packs, "_MAX_PACKS_PER_JOB"),
            (old.resolve(), "_MAX_CANDIDATE_TREE_ENTRIES"),
        ):
            with self.subTest(target=target):
                scanned, closed = [], []

                def entries():
                    for index in range(3):
                        scanned.append(index)
                        yield SimpleNamespace(
                            path=str(target / str(index)),
                            name=str(index),
                            is_dir=lambda: False,
                        )
                    raise AssertionError("scan exceeded the admission budget")

                @contextmanager
                def bounded_scan():
                    try:
                        yield entries()
                    finally:
                        closed.append(True)

                def scandir(path):
                    return (
                        bounded_scan()
                        if not isinstance(path, int) and Path(path) == target
                        else original_scandir(path)
                    )

                with (
                    patch.object(cache, limit_name, 2),
                    patch.object(cache.os, "scandir", side_effect=scandir),
                ):
                    self.assertEqual(
                        prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
                    )
                self.assertEqual(scanned, [0, 1, 2])
                self.assertEqual(closed, [True])
                self.assertTrue(old.exists())

    def test_candidate_tree_budget_counts_nested_entries_and_accepts_exact_limit(
        self,
    ) -> None:
        old = self._candidate("old")
        nested = old / "nested"
        nested.mkdir()
        (nested / "first.wav").write_bytes(b"first")
        (nested / "second.wav").write_bytes(b"second")
        with patch.object(cache, "_MAX_CANDIDATE_TREE_ENTRIES", 3):
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
            )
        self.assertEqual((nested / "first.wav").read_bytes(), b"first")
        with patch.object(cache, "_MAX_CANDIDATE_TREE_ENTRIES", 4):
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs),
                (old.resolve(),),
            )
        self.assertFalse(old.exists())

    def test_queued_candidate_directory_alias_defers_cleanup_before_descent(
        self,
    ) -> None:
        old = self._candidate("old")
        child = old / "nested"
        child.mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "reference.wav").write_bytes(b"outside audio")
        canonical_child = child.resolve()
        saved = self.root.parent / "saved-child"
        replaced, scanned = [], []
        original_is_dir, original_scandir = cache.S_ISDIR, cache.os.scandir

        def replace_after_inspection(mode):
            is_directory = original_is_dir(mode)
            if is_directory and not replaced:
                child.rename(saved)
                symlink_or_skip(child, outside, target_is_directory=True)
                replaced.append(True)
            return is_directory

        def forbid_alias_descent(path):
            if Path(path) == canonical_child:
                scanned.append(path)
                raise AssertionError("queued directory alias was followed")
            return original_scandir(path)

        with (
            patch.object(cache, "S_ISDIR", side_effect=replace_after_inspection),
            patch.object(cache.os, "scandir", side_effect=forbid_alias_descent),
        ):
            self.assertEqual(
                prune_obsolete_voice_candidate_caches(self.root, self.jobs), ()
            )
        self.assertEqual(replaced, [True])
        self.assertEqual(scanned, [])
        self.assertTrue(old.exists())
        self.assertEqual((outside / "reference.wav").read_bytes(), b"outside audio")

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
        original_open = open

        def grow_before_read(path, mode="r", *args, **kwargs):
            if path == reference and mode in {"r", "rb"}:
                with original_open(reference, "wb") as destination:
                    destination.write(b"{}" + b" " * 256)
            return original_open(path, mode, *args, **kwargs)

        with (
            patch.object(cache, "_MAX_REFERENCE_DOCUMENT_BYTES", 128),
            patch("vntts.path_safety.open", side_effect=grow_before_read, create=True),
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

    def test_invalid_path_defers_cleanup_but_outside_paths_do_not(self) -> None:
        old = self._candidate("old")
        job = self.jobs / ("a" * 24)
        job.mkdir()
        reference = job / "voice-plan.json"
        invalid = str(old / "reference\x00.wav")
        for location in ("protected", "saved"):
            with self.subTest(location=location):
                if not old.exists():
                    old = self._candidate("old")
                reference.write_text(
                    json.dumps(
                        {"voice_manifest": invalid if location == "saved" else ""}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    prune_obsolete_voice_candidate_caches(
                        self.root,
                        self.jobs,
                        protected_paths=(invalid,) if location == "protected" else (),
                    ),
                    (),
                )
                self.assertTrue(old.exists())
        if not old.exists():
            old = self._candidate("old")
        reference.write_text(
            json.dumps({"voice_manifest": str(self.jobs / "outside.json")}),
            encoding="utf-8",
        )
        self.assertEqual(
            prune_obsolete_voice_candidate_caches(self.root, self.jobs),
            (old.resolve(),),
        )

    def _candidate(self, name: str) -> Path:
        directory = self.root / hashlib.sha256(name.encode()).hexdigest()[:24]
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        return directory
