import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.cli import main
from vntts.authoring.listening_import import (
    IMPORT_SCHEMA,
    ListeningImportError,
    import_listening_session,
    inspect_listening_session,
)


def write_listening_fixture(root):
    session_root = root / "listening-session"
    audio_root = session_root / "audio"
    audio_root.mkdir(parents=True)
    source_audio = {}
    for side, frequency in (("a", 3), ("b", 5)):
        samples = np.sin(np.linspace(0, frequency * np.pi, 800, dtype=np.float32)) * 0.1
        source_audio[side] = root / f"source-{side}.wav"
        write_pcm16_wav(source_audio[side], samples, 16_000)
        write_pcm16_wav(audio_root / f"trial-0001-{side}.wav", samples, 16_000)
    source_report = root / "source-report.json"
    source_report.write_text('{"synthetic": true}\n', encoding="utf-8")
    sources = [{"path": str(source_report.resolve()), "sha256": sha256_file(source_report)}]
    source_hash = hashlib.sha256(
        json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    key = {
        "schema": "r1999.model-listening-key",
        "schema_version": 1,
        "created_at": "2026-08-15T09:00:00+00:00",
        "source_kind": "model-reports",
        "source_sha256": source_hash,
        "sources": sources,
        "models": [
            {
                "model_id": "provider/model-one",
                "provider": "provider",
                "model": "model-one",
                "reports": ["/legacy/one.json"],
            },
            {
                "model_id": "provider/model-two",
                "provider": "provider",
                "model": "model-two",
                "reports": ["/legacy/two.json"],
            },
        ],
        "assignments": [
            {
                "trial_id": "trial-0001",
                "a": {
                    "model_id": "provider/model-one",
                    "source": str(source_audio["a"].resolve()),
                },
                "b": {
                    "model_id": "provider/model-two",
                    "source": str(source_audio["b"].resolve()),
                },
            }
        ],
    }
    key_path = session_root / ".blind-key.json"
    key_path.write_text(json.dumps(key, sort_keys=True), encoding="utf-8")
    session = {
        "schema": "r1999.model-listening-session",
        "schema_version": 1,
        "created_at": "2026-08-15T09:00:00+00:00",
        "updated_at": "2026-08-15T09:01:00+00:00",
        "source_kind": "model-reports",
        "source_sha256": source_hash,
        "blind_key_sha256": sha256_file(key_path),
        "seed": 42,
        "decision_mode": "preference-only",
        "trial_count": 1,
        "completed_count": 1,
        "trials": [
            {
                "trial_id": "trial-0001",
                "queue_id": "legacy:line",
                "line_id": None,
                "text_sha256": "3" * 64,
                "text": "Synthetic line",
                "audio": {
                    "a": "audio/trial-0001-a.wav",
                    "b": "audio/trial-0001-b.wav",
                },
                "rating": {
                    "preference": "a",
                    "reviewed_at": "2026-08-15T09:01:00+00:00",
                },
            }
        ],
    }
    session_path = session_root / "session.json"
    session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")
    report = {
        "schema": "r1999.model-listening-report",
        "schema_version": 1,
        "generated_at": "2026-08-15T09:02:00+00:00",
        "session": str(session_path.resolve()),
        "complete": True,
        "completed_trials": 1,
        "pending_trials": 0,
        "manual_selection_required": True,
        "models": [
            {
                "model_id": "provider/model-one",
                "provider": "provider",
                "model": "model-one",
                "reviewed_trials": 1,
                "preference": {"wins": 1, "losses": 0, "ties": 0, "rate": 1.0},
                "rank": 1,
            },
            {
                "model_id": "provider/model-two",
                "provider": "provider",
                "model": "model-two",
                "reviewed_trials": 1,
                "preference": {"wins": 0, "losses": 1, "ties": 0, "rate": 0.0},
                "rank": 2,
            },
        ],
        "pairwise": [
            {
                "left_model": "provider/model-one",
                "right_model": "provider/model-two",
                "trials": 1,
                "left_wins": 1,
                "right_wins": 0,
                "ties": 0,
            }
        ],
    }
    report_path = session_root / "report.json"
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return session_root


class ListeningImportTest(unittest.TestCase):
    def test_source_mutation_during_copy_aborts_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            session_path = source / "session.json"
            original_copy = __import__("shutil").copy2
            mutated = False

            def mutate_after_report_copy(source_path, destination):
                nonlocal mutated
                result = original_copy(source_path, destination)
                if Path(source_path).name == "report.json" and not mutated:
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    session["updated_at"] = "2026-08-15T10:00:00+00:00"
                    session_path.write_text(
                        json.dumps(session, sort_keys=True), encoding="utf-8"
                    )
                    mutated = True
                return result

            with patch(
                "vntts.authoring.listening_import.shutil.copy2",
                side_effect=mutate_after_report_copy,
            ), self.assertRaisesRegex(ListeningImportError, "retry when idle"):
                import_listening_session(source, root / "app-data")

            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_import_preserves_session_key_report_audio_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            source_hashes = {
                path.relative_to(source).as_posix(): sha256_file(path)
                for path in source.rglob("*")
                if path.is_file()
            }

            inspection = inspect_listening_session(source)
            first = import_listening_session(source, root / "app-data")
            second = import_listening_session(source, root / "app-data")

            self.assertEqual(inspection.trial_count, 1)
            self.assertEqual(inspection.audio_count, 2)
            self.assertEqual(first.manifest["schema"], IMPORT_SCHEMA)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.destination, second.destination)
            self.assertTrue((first.destination / ".blind-key.json").is_file())
            self.assertEqual(
                source_hashes,
                {
                    path.relative_to(source).as_posix(): sha256_file(path)
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )

    def test_rejects_changed_key_path_escape_and_inconsistent_report(self):
        mutations = ("key", "path", "audio", "report")
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                source = write_listening_fixture(root)
                if mutation == "key":
                    key_path = source / ".blind-key.json"
                    key = json.loads(key_path.read_text(encoding="utf-8"))
                    key["assignments"][0]["a"]["model_id"] = "provider/model-two"
                    key_path.write_text(json.dumps(key, sort_keys=True), encoding="utf-8")
                elif mutation == "path":
                    session_path = source / "session.json"
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    session["trials"][0]["audio"]["a"] = "../escape.wav"
                    session_path.write_text(
                        json.dumps(session, sort_keys=True), encoding="utf-8"
                    )
                elif mutation == "audio":
                    (source / "audio/trial-0001-a.wav").write_bytes(b"tampered")
                else:
                    report_path = source / "report.json"
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    report["completed_trials"] = 0
                    report_path.write_text(
                        json.dumps(report, sort_keys=True), encoding="utf-8"
                    )

                with self.assertRaises(ListeningImportError):
                    inspect_listening_session(source)

    def test_changed_session_after_import_is_a_hard_conflict(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            first = import_listening_session(source, root / "app-data")
            imported_session_hash = sha256_file(first.destination / "session.json")
            session_path = source / "session.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["updated_at"] = "2026-08-15T10:00:00+00:00"
            session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ListeningImportError, "changed after import"):
                import_listening_session(source, root / "app-data")

            self.assertEqual(
                sha256_file(first.destination / "session.json"), imported_session_hash
            )

    def test_cli_inspection_is_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                result = main(["inspect-listening", str(source)])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["trial_count"], 1)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                [
                    "listening-session",
                    "source-a.wav",
                    "source-b.wav",
                    "source-report.json",
                ],
            )


if __name__ == "__main__":
    unittest.main()
