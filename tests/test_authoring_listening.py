import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from tests.test_authoring_listening_import import write_listening_fixture
from vntts.authoring.listening import (
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session,
    create_listening_session_from_reports,
    ensure_listening_report,
    listening_progress,
    load_listening_session,
    next_pending_trial,
    record_trial_preference,
)
from vntts.authoring.listening_import import import_listening_session

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.listening_ui import ModelListeningDialog
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QPoint = None
    Qt = None
    QTest = None
    QMediaPlayer = None
    ModelListeningDialog = None


def write_model_reports(root, *, item_count=2):
    reports = []
    for model_index, model_id in enumerate(("synthetic/one", "synthetic/two"), start=1):
        samples = []
        for item_index in range(item_count):
            text = f"Shared listening line {item_index} ..."
            if model_index == 2:
                text = text.replace("...", "…")
            audio = root / model_id.replace("/", "-") / f"sample-{item_index}.wav"
            values = np.full(800, model_index * 0.05, dtype=np.float32)
            write_pcm16_wav(audio, values, 16_000)
            samples.append(
                {
                    "id": f"sample-{item_index}",
                    "character": "Voice",
                    "text": text,
                    "audio": str(audio),
                }
            )
        report = root / f"report-{model_index}.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "vntts.voice-model-report",
                    "schema_version": 1,
                    "model_id": model_id,
                    "provider": "synthetic",
                    "backend": "synthetic",
                    "model": model_id.rsplit("/", 1)[-1],
                    "samples": samples,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        reports.append(report)
    return reports


class AuthoringListeningTest(unittest.TestCase):
    def test_creates_deterministic_blind_trials_without_public_model_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root)
            first = create_listening_session_from_reports(reports, root / "first", seed=17)
            second = create_listening_session_from_reports(reports, root / "second", seed=17)
            first_session = load_listening_session(first)
            second_session = load_listening_session(second)
            public = first.read_text(encoding="utf-8")

        self.assertEqual(first_session["trial_count"], 2)
        self.assertEqual(
            [(trial["queue_id"], trial["audio"]) for trial in first_session["trials"]],
            [(trial["queue_id"], trial["audio"]) for trial in second_session["trials"]],
        )
        self.assertNotIn("synthetic/one", public)
        self.assertNotIn("synthetic/two", public)

    def test_starts_from_vntts_benchmark_aggregate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=1)
            benchmark = root / "benchmark.json"
            benchmark.write_text(
                json.dumps(
                    {
                        "schema": "vntts.voice-model-benchmark",
                        "schema_version": 1,
                        "reports": [str(report) for report in reports],
                    }
                ),
                encoding="utf-8",
            )

            session = create_listening_session(benchmark, root / "session", seed=3)
            trial_count = load_listening_session(session)["trial_count"]

        self.assertEqual(trial_count, 1)

    def test_scores_resumes_overwrites_and_builds_ranked_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root), root / "session", seed=5
            )
            key = json.loads(
                session_path.with_name(".blind-key.json").read_text(encoding="utf-8")
            )
            assignments = {item["trial_id"]: item for item in key["assignments"]}
            for trial in load_listening_session(session_path)["trials"]:
                assignment = assignments[trial["trial_id"]]
                winner = "a" if assignment["a"]["model_id"] == "synthetic/one" else "b"
                record_trial_preference(session_path, trial["trial_id"], winner)
            first_trial = load_listening_session(session_path)["trials"][0]
            with self.assertRaisesRegex(ModelListeningError, "already rated"):
                record_trial_preference(session_path, first_trial["trial_id"], "tie")
            record_trial_preference(
                session_path, first_trial["trial_id"], first_trial["rating"]["preference"], overwrite=True
            )
            report = aggregate_listening_report(
                session_path, session_path.with_name("report.json")
            )
            resumed_progress = listening_progress(load_listening_session(session_path))

        self.assertEqual(resumed_progress, (2, 2))
        self.assertEqual(report["models"][0]["model_id"], "synthetic/one")
        self.assertEqual(report["models"][0]["preference"]["wins"], 2)
        self.assertEqual(report["pairwise"][0]["trials"], 2)

    def test_legacy_import_load_and_current_report_are_hash_preserving(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            imported = import_listening_session(source, root / "app-data").destination
            session_path = imported / "session.json"
            protected = [
                session_path,
                imported / ".blind-key.json",
                imported / "report.json",
                *sorted((imported / "audio").glob("*.wav")),
            ]
            before = {path.relative_to(imported): sha256_file(path) for path in protected}
            for name in ("source-a.wav", "source-b.wav", "source-report.json"):
                (root / name).unlink()

            session = load_listening_session(session_path)
            report = ensure_listening_report(session_path)
            after = {path.relative_to(imported): sha256_file(path) for path in protected}

        self.assertEqual(listening_progress(session), (1, 1))
        self.assertIsNone(next_pending_trial(session))
        self.assertTrue(report["complete"])
        self.assertEqual(before, after)

    def test_incomplete_legacy_import_resumes_without_key_or_alias_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            session_path = source / "session.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["trials"][0]["rating"] = None
            session["completed_count"] = 0
            session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")
            (source / "report.json").unlink()
            imported = import_listening_session(source, root / "app-data").destination
            imported_session = imported / "session.json"
            key_hash = sha256_file(imported / ".blind-key.json")
            audio_hashes = {
                path.name: sha256_file(path) for path in (imported / "audio").glob("*.wav")
            }
            for name in ("source-a.wav", "source-b.wav", "source-report.json"):
                (root / name).unlink()

            trial = next_pending_trial(load_listening_session(imported_session))
            record_trial_preference(imported_session, trial["trial_id"], "tie")
            report = aggregate_listening_report(
                imported_session, imported_session.with_name("report.json")
            )
            preserved_key_hash = sha256_file(imported / ".blind-key.json")
            preserved_audio_hashes = {
                path.name: sha256_file(path) for path in (imported / "audio").glob("*.wav")
            }

        self.assertTrue(report["complete"])
        self.assertEqual(preserved_key_hash, key_hash)
        self.assertEqual(preserved_audio_hashes, audio_hashes)

    def test_rejects_progress_key_and_path_tamper(self):
        for mutation in ("progress", "key", "path"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                session_path = create_listening_session_from_reports(
                    write_model_reports(root, item_count=1), root / "session"
                )
                if mutation == "key":
                    key_path = session_path.with_name(".blind-key.json")
                    key_path.write_text(key_path.read_text() + " ", encoding="utf-8")
                else:
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    if mutation == "progress":
                        session["completed_count"] = 1
                    else:
                        session["trials"][0]["audio"]["a"] = "../escape.wav"
                    session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")

                with self.assertRaises(ModelListeningError):
                    if mutation == "key":
                        aggregate_listening_report(session_path)
                    else:
                        load_listening_session(session_path)


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class AuthoringListeningDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, ModelListeningDialog):
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def create_dialog(self, root):
        session = create_listening_session_from_reports(
            write_model_reports(root, item_count=1), root / "session"
        )
        dialog = ModelListeningDialog(session, auto_play=False)
        dialog.player = Mock()
        return session, dialog

    def test_requires_both_samples_then_saves_and_completes(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            dialog.play("a")
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertFalse(dialog.prefer_a.isEnabled())
            dialog.play("b")
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertTrue(dialog.prefer_a.isEnabled())
            dialog.save_preference("a")

            self.assertEqual(load_listening_session(session)["completed_count"], 1)
            self.assertIsNone(dialog.current_trial)
            self.assertTrue(session.with_name("report.json").is_file())
            dialog.deleteLater()

    def test_autoplays_a_then_b_and_tracks_controls(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.start_auto_playback()
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(dialog.started_sides, {"a"})
            dialog.media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.application.processEvents()
            dialog.playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(dialog.started_sides, {"a", "b"})
            self.assertTrue(dialog.tie.isEnabled())
            dialog.toggle_playback()
            dialog.player.pause.assert_called_once_with()
            self.assertEqual(dialog.stop.text(), "Continue")
            dialog.deleteLater()

    def test_seek_skip_and_track_click(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.player.position.return_value = 2_000
            dialog.player.duration.return_value = 120_000
            dialog.duration_changed(120_000)
            dialog.position_changed(65_000)
            dialog.seek_to(90_000)
            dialog.skip_by(5_000)
            self.assertEqual(dialog.time.text(), "0:07 / 2:00")
            dialog.show()
            self.application.processEvents()
            QTest.mouseClick(
                dialog.seek,
                Qt.MouseButton.LeftButton,
                pos=QPoint(dialog.seek.width() * 3 // 4, dialog.seek.height() // 2),
            )
            self.assertAlmostEqual(dialog.seek.value(), 90_000, delta=2_000)
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
