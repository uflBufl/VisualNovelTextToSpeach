import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import soundfile as sf
from vntts_artifacts.audio import write_pcm16_wav

from tests.symlink_support import symlink_or_skip
from vntts.audio_cache import PersistentAudioCache
from vntts.audio_cache_migration import main, migrate_legacy_cache
from vntts.audio_store import SharedAudioStore
from vntts.authoring.bulk_generation import generated_mono_pcm

_KEY = "a" * 48
_OTHER = "b" * 48
_PROVENANCE = dict(
    backend="moss-tts",
    model="recorded-original-model-revision",
    sample_rate=48_000,
    sample_rate_evidence="Original generation model config: sampling_rate=48000",
)


class LegacyAudioMigrationTest(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory) / "audio"
        root.mkdir()
        return root

    def test_dry_run_validates_without_writing_or_traversing_other_caches(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125, -0.125]))
            prompt = root / "prompt-codes"
            prompt.mkdir()
            np.save(prompt / f"{_OTHER}.npy", np.array([1, 2, 3]))
            before = legacy.read_bytes()
            report = migrate_legacy_cache(root, **_PROVENANCE)
            self.assertEqual(report["counts"], {"ready": 1, "preserved_unknown": 1})
            self.assertEqual(legacy.read_bytes(), before)
            self.assertFalse((root / "objects").exists())
            self.assertTrue((prompt / f"{_OTHER}.npy").exists())

    def test_apply_preserves_keys_and_survives_restart_and_repeat(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            audio = np.array([[0.125, -0.125], [0.25, -0.25]], dtype=np.float32)
            np.save(root / f"{_KEY}.npy", audio)
            report = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(report["counts"], {"migrated": 1})
            self.assertFalse((root / f"{_KEY}.npy").exists())
            cache = PersistentAudioCache(root, sample_rate=48_000, max_entries=0)
            np.testing.assert_array_equal(cache.get_existing(_KEY), audio)
            self.assertIsNone(cache.put(_OTHER, audio))
            repeated = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(repeated["counts"], {"already_wav": 1})
            self.assertEqual(report["entries"][0]["wav"], repeated["entries"][0]["wav"])

    def test_unproven_sample_rate_never_converts(self):
        for evidence in (
            {"sample_rate": None},
            {"sample_rate_evidence": ""},
            {"model": ""},
        ):
            with self.subTest(evidence=evidence), TemporaryDirectory() as directory:
                root = self.make_root(directory)
                legacy = root / f"{_KEY}.npy"
                np.save(legacy, np.array([0.125]))
                report = migrate_legacy_cache(
                    root, **(_PROVENANCE | evidence), apply=True
                )
                self.assertEqual(report["counts"], {"preserved_unproven": 1})
                self.assertTrue(legacy.exists())
                self.assertFalse((root / "objects").exists())

    def test_mixed_root_uses_only_individual_key_provenance(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            unproven = "c" * 48
            for key in (_KEY, _OTHER, unproven):
                np.save(root / f"{key}.npy", np.array([0.125]))
            proof = {
                _KEY: _PROVENANCE,
                _OTHER: _PROVENANCE
                | {
                    "backend": "qwen-tts",
                    "sample_rate": 24_000,
                    "model": "original-qwen-model",
                    "sample_rate_evidence": "original Qwen wrapper rate",
                },
            }
            report = migrate_legacy_cache(
                root, **_PROVENANCE, key_provenance=proof, apply=True
            )
            self.assertEqual(report["counts"], {"migrated": 2, "preserved_unproven": 1})
            self.assertTrue((root / f"{unproven}.npy").exists())
            for key, rate in ((_KEY, 48_000), (_OTHER, 24_000)):
                cache = PersistentAudioCache(root, sample_rate=rate, max_entries=0)
                np.testing.assert_array_equal(cache.get_existing(key), [0.125])
            repeated = migrate_legacy_cache(
                root, **_PROVENANCE, key_provenance=proof, apply=True
            )
            self.assertEqual(
                repeated["counts"], {"already_wav": 2, "preserved_unproven": 1}
            )

    def test_unknown_corrupt_and_symlink_entries_are_preserved(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / f"{_KEY}.npy").write_bytes(b"broken")
            (root / "unknown.npy").write_bytes(b"not known synthesis data")
            outside = Path(directory) / "original.npy"
            np.save(outside, np.array([0.125]))
            symlink_or_skip(root / f"{_OTHER}.npy", outside)
            before = outside.read_bytes()
            report = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(
                report["counts"], {"preserved_error": 2, "preserved_unknown": 1}
            )
            self.assertEqual(outside.read_bytes(), before)
            self.assertTrue((root / f"{_OTHER}.npy").is_symlink())
            self.assertEqual((root / f"{_KEY}.npy").read_bytes(), b"broken")

    def test_unrecognized_or_symlink_roots_are_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / "prompt-codes"
            prompts.mkdir()
            alias = root / "audio"
            symlink_or_skip(alias, prompts, target_is_directory=True)
            for invalid in (prompts, alias):
                with self.subTest(root=invalid), self.assertRaises(ValueError):
                    migrate_legacy_cache(invalid, **_PROVENANCE, apply=True)
            self.assertEqual(list(prompts.iterdir()), [])

    def test_conflicting_saved_wav_and_legacy_both_survive(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            cache = PersistentAudioCache(root, sample_rate=48_000)
            saved = cache.put(_KEY, [0.25])
            binding = list(SharedAudioStore(root).iter_bindings())
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125]))
            before = legacy.read_bytes()
            dry_run = migrate_legacy_cache(root, **_PROVENANCE)
            self.assertEqual(dry_run["counts"], {"preserved_error": 1})
            report = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(report["counts"], {"preserved_error": 1})
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(list(SharedAudioStore(root).iter_bindings()), binding)
            self.assertEqual(cache.store.get(_KEY), saved)
            np.testing.assert_array_equal(cache.get_existing(_KEY), [0.25])

    def test_interrupted_publication_is_recoverable_on_next_run(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125]))
            publish = SharedAudioStore.put

            def interrupt(store, *arguments):
                publish(store, *arguments)
                raise RuntimeError("process interruption after durable publication")

            with (
                patch.object(SharedAudioStore, "put", interrupt),
                self.assertRaises(RuntimeError),
            ):
                migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertTrue(legacy.exists())
            self.assertIsNotNone(SharedAudioStore(root).get(_KEY))
            self.assertEqual(
                migrate_legacy_cache(root, **_PROVENANCE)["counts"], {"ready": 1}
            )
            report = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(report["counts"], {"migrated": 1})
            self.assertFalse(legacy.exists())

    def test_failed_readback_retains_original(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125]))
            before = legacy.read_bytes()
            with patch.object(PersistentAudioCache, "get_existing", return_value=None):
                report = migrate_legacy_cache(root, **_PROVENANCE, apply=True)
            self.assertEqual(report["counts"], {"preserved_error": 1})
            self.assertEqual(legacy.read_bytes(), before)

    def test_existing_only_lookup_never_migrates_legacy_or_creates_files(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125]))
            for limit in (0, 256):
                self.assertIsNone(
                    PersistentAudioCache(
                        root, sample_rate=48_000, max_entries=limit
                    ).get_existing(_KEY)
                )
            self.assertEqual(list(root.iterdir()), [legacy])

    def test_disappearing_saved_binding_is_reported_without_claiming_success(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            saved = PersistentAudioCache(root, sample_rate=48_000).put(_KEY, [0.125])
            with patch.object(SharedAudioStore, "get", return_value=None):
                report = migrate_legacy_cache(root, **_PROVENANCE)
            self.assertEqual(report["counts"], {"preserved_error": 1})
            self.assertTrue(saved.exists())

    def test_cli_defaults_to_report_and_preserves_unproven_data(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            np.save(root / f"{_KEY}.npy", np.array([0.125]))
            manifest = Path(directory) / "roots.json"
            manifest.write_text(json.dumps([dict(directory=str(root), **_PROVENANCE)]))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--manifest", str(manifest)]), 0)
            self.assertEqual(json.loads(output.getvalue())[0]["counts"], {"ready": 1})
            self.assertTrue((root / f"{_KEY}.npy").exists())

    def test_cli_validates_all_roots_before_apply(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            np.save(legacy, np.array([0.125]))
            manifest = Path(directory) / "roots.json"
            manifest.write_text(
                json.dumps(
                    [
                        dict(directory=str(root), **_PROVENANCE),
                        dict(directory=str(root / "missing" / "audio"), **_PROVENANCE),
                    ]
                )
            )
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(["--manifest", str(manifest), "--apply"])
            self.assertTrue(legacy.exists())
            self.assertFalse((root / "objects").exists())

    def test_completed_wav_is_reused_as_one_inode_without_reencoding(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            audio = np.array([-1.0, -0.1, 0.1, 1.0], dtype=np.float32)
            np.save(legacy, audio)
            completed = Path(directory) / "completed.wav"
            write_pcm16_wav(completed, audio, 48_000)
            proof = {
                _KEY: _PROVENANCE
                | {
                    "completed_wav": {
                        "path": str(completed),
                        "sha256": sha256(completed.read_bytes()).hexdigest(),
                    }
                }
            }
            dry_run = migrate_legacy_cache(root, **_PROVENANCE, key_provenance=proof)
            self.assertEqual(dry_run["counts"], {"ready": 1})
            self.assertFalse((root / "objects").exists())
            with patch.object(
                PersistentAudioCache,
                "_put_prepared",
                side_effect=AssertionError("Must reuse final WAV"),
            ):
                report = migrate_legacy_cache(
                    root, **_PROVENANCE, key_provenance=proof, apply=True
                )
            self.assertEqual(report["counts"], {"migrated": 1})
            saved = SharedAudioStore(root).get(_KEY)
            self.assertTrue(saved.samefile(completed))
            self.assertEqual(len(list((root / "objects").glob("*.wav"))), 1)
            self.assertFalse(legacy.exists())
            repeated = migrate_legacy_cache(
                root, **_PROVENANCE, key_provenance=proof, apply=True
            )
            self.assertEqual(repeated["counts"], {"already_wav": 1})
            self.assertTrue(SharedAudioStore(root).get(_KEY).samefile(completed))
            proof[_KEY]["completed_wav"]["sha256"] = "0" * 64
            conflicting = migrate_legacy_cache(
                root, **_PROVENANCE, key_provenance=proof, apply=True
            )
            self.assertEqual(conflicting["counts"], {"preserved_error": 1})
            self.assertTrue(SharedAudioStore(root).get(_KEY).samefile(completed))

    def test_unsafe_completed_wav_proof_preserves_original(self):
        for damage in (
            "checksum",
            "pcm",
            "rate",
            "symlink",
            "float",
            "stereo",
            "conflict",
        ):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = self.make_root(directory)
                legacy = root / f"{_KEY}.npy"
                audio = np.array([-0.1, 0.1], dtype=np.float32)
                np.save(legacy, audio)
                original = legacy.read_bytes()
                completed = Path(directory) / "completed.wav"
                write_pcm16_wav(
                    completed,
                    audio if damage != "pcm" else audio * 0.5,
                    24_000 if damage == "rate" else 48_000,
                )
                if damage == "float":
                    sf.write(completed, audio, 48_000, format="WAV", subtype="FLOAT")
                elif damage == "stereo":
                    sf.write(
                        completed,
                        np.column_stack((audio, audio)),
                        48_000,
                        format="WAV",
                        subtype="PCM_16",
                    )
                digest = sha256(completed.read_bytes()).hexdigest()
                if damage == "symlink":
                    alias = completed.with_name("alias.wav")
                    symlink_or_skip(alias, completed)
                    completed = alias
                binding = None
                if damage == "conflict":
                    PersistentAudioCache(root, sample_rate=48_000).put(_KEY, [0.5])
                    binding = list(SharedAudioStore(root).iter_bindings())
                proof = {
                    _KEY: _PROVENANCE
                    | {
                        "completed_wav": {
                            "path": str(completed),
                            "sha256": "0" * 64 if damage == "checksum" else digest,
                        }
                    }
                }
                for apply in (False, True):
                    report = migrate_legacy_cache(
                        root, **_PROVENANCE, key_provenance=proof, apply=apply
                    )
                    self.assertEqual(report["counts"], {"preserved_error": 1})
                    self.assertEqual(legacy.read_bytes(), original)
                if binding is None:
                    self.assertFalse((root / "objects").exists())
                else:
                    self.assertEqual(
                        list(SharedAudioStore(root).iter_bindings()), binding
                    )

    def test_stereo_legacy_reuses_exact_final_generation_downmix(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            audio = np.array(
                [[-2.0, 1.0], [0.2, 0.8], [-0.8, 0.4], [1.0, 1.0]], dtype=np.float32
            )
            np.save(legacy, audio)
            completed = Path(directory) / "completed.wav"
            write_pcm16_wav(completed, generated_mono_pcm(audio), 48_000)
            proof = {
                _KEY: _PROVENANCE
                | {
                    "completed_wav": {
                        "path": str(completed),
                        "sha256": sha256(completed.read_bytes()).hexdigest(),
                    }
                }
            }
            for apply in (False, True):
                report = migrate_legacy_cache(
                    root, **_PROVENANCE, key_provenance=proof, apply=apply
                )
                self.assertEqual(
                    report["counts"], {"migrated" if apply else "ready": 1}
                )
            self.assertFalse(legacy.exists())
            self.assertTrue(SharedAudioStore(root).get(_KEY).samefile(completed))
            self.assertEqual(len(list((root / "objects").glob("*.wav"))), 1)

    def test_completed_wav_retry_after_interruption_keeps_same_inode(self):
        with TemporaryDirectory() as directory:
            root = self.make_root(directory)
            legacy = root / f"{_KEY}.npy"
            audio = np.array([-0.1, 0.1], dtype=np.float32)
            np.save(legacy, audio)
            completed = Path(directory) / "completed.wav"
            write_pcm16_wav(completed, audio, 48_000)
            proof = {
                _KEY: _PROVENANCE
                | {
                    "completed_wav": {
                        "path": str(completed),
                        "sha256": sha256(completed.read_bytes()).hexdigest(),
                    }
                }
            }
            with patch.object(PersistentAudioCache, "get_existing", return_value=None):
                interrupted = migrate_legacy_cache(
                    root, **_PROVENANCE, key_provenance=proof, apply=True
                )
            self.assertEqual(interrupted["counts"], {"preserved_error": 1})
            self.assertTrue(legacy.exists())
            self.assertTrue(SharedAudioStore(root).get(_KEY).samefile(completed))
            retry = migrate_legacy_cache(
                root, **_PROVENANCE, key_provenance=proof, apply=True
            )
            self.assertEqual(retry["counts"], {"migrated": 1})
            self.assertFalse(legacy.exists())
            self.assertTrue(SharedAudioStore(root).get(_KEY).samefile(completed))


if __name__ == "__main__":
    unittest.main()
