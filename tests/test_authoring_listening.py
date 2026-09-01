import hashlib
import json
import os
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from tests.test_authoring_listening_import import write_listening_fixture
from vntts.authoring.listening import (
    REPORT_SCHEMA,
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
from vntts.authoring.listening_cli import main as listening_main
from vntts.authoring.listening_import import import_listening_session
from vntts.authoring.pcm_playback import PcmClip, PlaybackSnapshot

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.listening_ui import ModelListeningDialog
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QPoint = None
    QCloseEvent = None
    Qt = None
    QTimer = None
    QTest = None
    ModelListeningDialog = None


class FakePlayback:
    def __init__(self):
        self.sample_rate = 16_000
        self.channels = 1
        self.token = 0
        self.clip = PcmClip(np.empty((0, 1), dtype=np.float32), self.sample_rate)
        self.position = 0
        self.started = False
        self.playing = False
        self.finished = False
        self.underflowed = False
        self.error = None
        self.play_calls = []
        self.pause_calls = 0
        self.closed = False

    def load(self, path):
        with wave.open(str(path), "rb") as source:
            frames = source.getnframes()
        return PcmClip(np.zeros((frames, 1), dtype=np.float32), self.sample_rate)

    def play(self, clip, *, position_frames=0):
        self.token += 1
        self.clip = clip
        self.position = position_frames
        self.started = False
        self.playing = True
        self.finished = False
        self.underflowed = False
        self.error = None
        self.play_calls.append((clip, position_frames))
        return self.token

    def finish(self, *, underflowed=False):
        self.position = self.clip.frames
        self.started = True
        self.playing = False
        self.finished = True
        self.underflowed = underflowed

    def snapshot(self):
        return PlaybackSnapshot(
            token=self.token,
            position_frames=self.position,
            total_frames=self.clip.frames,
            started=self.started,
            playing=self.playing,
            finished=self.finished,
            underflowed=self.underflowed,
            error=self.error,
        )

    def pause(self):
        self.pause_calls += 1
        self.playing = False

    def resume(self):
        self.token += 1
        self.playing = True
        self.started = False
        self.finished = False
        return self.token

    def seek(self, position_frames):
        self.token += 1
        self.position = max(0, min(self.clip.frames, position_frames))
        self.finished = False
        return self.token

    def stop(self):
        self.token += 1
        self.playing = False
        self.finished = False

    def close(self):
        self.closed = True


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
                    "line_id": f"line-{item_index}",
                    "character": "Voice",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "audio": str(audio),
                    "audio_sha256": sha256_file(audio),
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
            first = create_listening_session_from_reports(
                reports, root / "first", seed=17
            )
            second = create_listening_session_from_reports(
                reports, root / "second", seed=17
            )
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

    def test_skips_noncomplete_outputs_and_filters_exact_sample_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=3)
            first = json.loads(reports[0].read_text(encoding="utf-8"))
            first["samples"][2] = {
                key: value
                for key, value in first["samples"][2].items()
                if key not in {"audio", "audio_sha256"}
            }
            first["samples"][2]["outcome"] = "limited"
            reports[0].write_text(json.dumps(first), encoding="utf-8")

            session = create_listening_session_from_reports(
                reports,
                root / "session",
                seed=3,
                sample_ids=("sample-1",),
            )
            trials = load_listening_session(session)["trials"]

            self.assertEqual(len(trials), 1)
            self.assertIn("sample-1", trials[0]["queue_id"])
            with self.assertRaisesRegex(ModelListeningError, "complete audio"):
                create_listening_session_from_reports(
                    reports,
                    root / "rejected",
                    sample_ids=("sample-2",),
                )

    def test_starts_from_strict_single_backend_tts_reports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=1)
            for index, report_path in enumerate(reports, start=1):
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["schema"] = "vntts.tts-benchmark-report"
                report["model_id"] = f"tts/model-{index}"
                report["backend"] = f"tts-backend-{index}"
                report_path.write_text(json.dumps(report), encoding="utf-8")

            session_path = create_listening_session_from_reports(
                reports, root / "session", seed=9
            )
            session = load_listening_session(session_path)

        self.assertEqual(session["trial_count"], 1)

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
                session_path,
                first_trial["trial_id"],
                first_trial["rating"]["preference"],
                overwrite=True,
            )
            report = aggregate_listening_report(
                session_path, session_path.with_name("report.json")
            )
            resumed_progress = listening_progress(load_listening_session(session_path))

        self.assertEqual(resumed_progress, (2, 2))
        self.assertEqual(report["models"][0]["model_id"], "synthetic/one")
        self.assertEqual(report["models"][0]["preference"]["wins"], 2)
        self.assertEqual(report["pairwise"][0]["trials"], 2)

    def test_neither_acceptable_is_not_counted_as_a_tie_or_win(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session", seed=5
            )
            trial = next_pending_trial(load_listening_session(session_path))
            record_trial_preference(session_path, trial["trial_id"], "neither")
            session = load_listening_session(session_path)
            report = aggregate_listening_report(session_path)

        self.assertEqual(session["trials"][0]["rating"]["preference"], "tie")
        self.assertEqual(session["trials"][0]["rating"]["acceptability"], "neither")
        self.assertEqual(
            [model["preference"]["rejections"] for model in report["models"]],
            [1, 1],
        )
        self.assertEqual(
            [model["preference"]["ties"] for model in report["models"]], [0, 0]
        )
        self.assertIsNone(report["models"][0]["preference"]["rate"])
        self.assertEqual(report["pairwise"][0]["neither_acceptable"], 1)

    def test_cli_records_neither_acceptable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session", seed=5
            )
            trial = next_pending_trial(load_listening_session(session_path))

            exit_code = listening_main(
                [
                    "score",
                    trial["trial_id"],
                    "--session",
                    str(session_path),
                    "--preference",
                    "neither",
                ]
            )
            rating = load_listening_session(session_path)["trials"][0]["rating"]

        self.assertEqual(exit_code, 0)
        self.assertEqual(rating["acceptability"], "neither")

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
            before = {
                path.relative_to(imported): sha256_file(path) for path in protected
            }
            for name in ("source-a.wav", "source-b.wav", "source-report.json"):
                (root / name).unlink()

            session = load_listening_session(session_path)
            report = ensure_listening_report(session_path)
            after = {
                path.relative_to(imported): sha256_file(path) for path in protected
            }

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
            session_path.write_text(
                json.dumps(session, sort_keys=True), encoding="utf-8"
            )
            (source / "report.json").unlink()
            imported = import_listening_session(source, root / "app-data").destination
            imported_session = imported / "session.json"
            key_hash = sha256_file(imported / ".blind-key.json")
            audio_hashes = {
                path.name: sha256_file(path)
                for path in (imported / "audio").glob("*.wav")
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
                path.name: sha256_file(path)
                for path in (imported / "audio").glob("*.wav")
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
                    session_path.write_text(
                        json.dumps(session, sort_keys=True), encoding="utf-8"
                    )

                with self.assertRaises(ModelListeningError):
                    if mutation == "key":
                        aggregate_listening_report(session_path)
                    else:
                        load_listening_session(session_path)

    def test_rejects_schema_less_reports_and_changed_or_invalid_audio(self):
        for mutation, pattern in (
            ("schema", "Unsupported model report schema"),
            ("checksum", "checksum changed"),
            ("not-wav", "supported WAV"),
        ):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                reports = write_model_reports(root, item_count=1)
                document = json.loads(reports[0].read_text(encoding="utf-8"))
                if mutation == "schema":
                    del document["schema"]
                    schema_less = reports[0].with_suffix(".txt")
                    schema_less.write_text(json.dumps(document), encoding="utf-8")
                    reports[0] = schema_less
                else:
                    audio = Path(document["samples"][0]["audio"])
                    if mutation == "checksum":
                        write_pcm16_wav(
                            audio, np.full(800, 0.3, dtype=np.float32), 16_000
                        )
                    else:
                        audio.write_bytes(b"not a wave")
                        document["samples"][0]["audio_sha256"] = sha256_file(audio)
                        reports[0].write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ModelListeningError, pattern):
                    create_listening_session_from_reports(reports, root / "session")

    def test_resume_rejects_alias_checksum_and_hidden_key_mode_changes(self):
        for mutation, pattern in (("alias", "checksum changed"), ("mode", "0600")):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                session_path = create_listening_session_from_reports(
                    write_model_reports(root, item_count=1), root / "session"
                )
                if mutation == "alias":
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    alias = session_path.parent / session["trials"][0]["audio"]["a"]
                    write_pcm16_wav(alias, np.full(800, 0.4, dtype=np.float32), 16_000)
                else:
                    session_path.with_name(".blind-key.json").chmod(0o644)
                with self.assertRaisesRegex(ModelListeningError, pattern):
                    load_listening_session(session_path)

    def test_imported_legacy_alias_is_bound_to_preservation_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            imported = import_listening_session(
                write_listening_fixture(root), root / "app-data"
            ).destination
            alias = next((imported / "audio").glob("*.wav"))
            write_pcm16_wav(alias, np.full(800, 0.4, dtype=np.float32), 16_000)
            with self.assertRaisesRegex(ModelListeningError, "checksum changed"):
                load_listening_session(imported / "session.json")

    def test_current_report_requires_current_schema_and_session_binding(self):
        for mutation in ("schema", "session"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                session_path = create_listening_session_from_reports(
                    write_model_reports(root, item_count=1), root / "session"
                )
                trial = load_listening_session(session_path)["trials"][0]
                report_path = session_path.with_name("report.json")
                record_trial_preference(
                    session_path, trial["trial_id"], "tie", report_path=report_path
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if mutation == "schema":
                    report["schema"] = "r1999.model-listening-report"
                else:
                    report["session"] = "/forged/session.json"
                report_path.write_text(json.dumps(report), encoding="utf-8")

                repaired = ensure_listening_report(session_path)

                self.assertEqual(repaired["schema"], REPORT_SCHEMA)
                self.assertEqual(repaired["session"], str(session_path.resolve()))

    def test_report_failure_explicitly_preserves_and_reports_saved_rating(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session"
            )
            trial_id = load_listening_session(session_path)["trials"][0]["trial_id"]
            report_path = session_path.with_name("report.json")
            from vntts.authoring import listening as listening_module

            original_write = listening_module.atomic_write_json

            def fail_report(path, value, **kwargs):
                if Path(path).resolve() == report_path.resolve():
                    raise OSError("synthetic report failure")
                return original_write(path, value, **kwargs)

            with (
                patch.object(
                    listening_module, "atomic_write_json", side_effect=fail_report
                ),
                self.assertRaisesRegex(ModelListeningError, "Preference was saved"),
            ):
                record_trial_preference(
                    session_path, trial_id, "a", report_path=report_path
                )

            saved = load_listening_session(session_path)
            self.assertEqual(saved["trials"][0]["rating"]["preference"], "a")
            self.assertFalse(report_path.exists())


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

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for the Qt worker")

    def create_dialog(self, root, **kwargs):
        item_count = kwargs.pop("item_count", 1)
        session = create_listening_session_from_reports(
            write_model_reports(root, item_count=item_count), root / "session"
        )
        playback = FakePlayback()
        dialog = ModelListeningDialog(
            session,
            auto_play=False,
            playback_factory=lambda: playback,
            **kwargs,
        )
        return session, dialog

    def test_requires_both_samples_then_saves_and_completes(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            dialog.play("a")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertFalse(dialog.prefer_a.isEnabled())
            dialog.play("b")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertTrue(dialog.prefer_a.isEnabled())
            self.assertTrue(dialog.neither.isEnabled())
            self.assertEqual(dialog.neither.shortcut().toString(), "Ctrl+Shift+N")
            dialog.save_preference("a")
            self.wait_for(lambda: not dialog._preference_active)

            self.assertEqual(load_listening_session(session)["completed_count"], 1)
            self.assertIsNone(dialog.current_trial)
            self.assertEqual(dialog.progress.text(), "Completed 1 of 1 | Remaining 0")
            self.assertEqual(dialog.trial_heading.text(), "All 1 trials reviewed")
            self.assertTrue(session.with_name("report.json").is_file())
            dialog.deleteLater()

    def test_trial_hierarchy_keyboard_accessibility_and_compact_layout(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory), item_count=3)
            dialog.show()
            dialog.resize(640, 400)
            dialog.activateWindow()
            self.application.processEvents()

            self.assertEqual(dialog.progress.text(), "Completed 0 of 3 | Remaining 3")
            self.assertEqual(dialog.progress_bar.maximum(), 3)
            self.assertEqual(dialog.progress_bar.value(), 0)
            self.assertEqual(dialog.trial_heading.text(), "Trial 1 of 3 | line-0")
            self.assertIn(
                "blind comparison",
                dialog.decision_context.values["model"].text(),
            )
            self.assertIn(
                "preference only",
                dialog.decision_context.values["effect"].text(),
            )
            self.assertEqual(
                dialog.dialogue.toPlainText(), "Shared listening line 0 ..."
            )
            self.assertIn("Decision locked", dialog.decision_reason.text())
            self.assertEqual(dialog.play_a.shortcut().toString(), "Ctrl+1")
            self.assertEqual(dialog.play_b.shortcut().toString(), "Ctrl+2")
            self.assertEqual(dialog.neither.shortcut().toString(), "Ctrl+Shift+N")
            for widget in (
                dialog.progress,
                dialog.progress_bar,
                dialog.trial_heading,
                dialog.dialogue,
                dialog.current_trial_card,
                dialog.now_playing,
                dialog.play_a,
                dialog.play_b,
                dialog.stop,
                dialog.seek,
                dialog.time,
                dialog.prefer_a,
                dialog.prefer_b,
                dialog.tie,
                dialog.neither,
                dialog.decision_reason,
                dialog.status,
            ):
                self.assertTrue(widget.accessibleName(), type(widget).__name__)

            dialog.play_a.setFocus()
            QTest.keyClick(dialog.play_a, Qt.Key.Key_Return)
            self.assertEqual(len(dialog.playback.play_calls), 1)
            dialog.playback.finish()
            dialog.poll_playback()
            dialog.play("b")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertTrue(dialog.prefer_a.isEnabled())
            self.assertIn("Decision ready", dialog.decision_reason.text())
            dialog.save_preference = Mock()
            dialog.prefer_a.setFocus()
            QTest.keyClick(dialog.prefer_a, Qt.Key.Key_Return)
            dialog.save_preference.assert_called_once_with("a")

            self.assertEqual(dialog.size().width(), 640)
            self.assertEqual(dialog.size().height(), 400)
            self.assertLessEqual(
                dialog.neither.geometry().bottom(), dialog.contentsRect().bottom()
            )
            dialog.deleteLater()

    def test_neither_acceptable_button_persists_distinct_verdict(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            dialog.completed_sides = {"a", "b"}
            dialog.set_preference_buttons_enabled(True)
            dialog.neither.click()
            self.wait_for(lambda: not dialog._preference_active)

            rating = load_listening_session(session)["trials"][0]["rating"]

        self.assertEqual(rating["preference"], "tie")
        self.assertEqual(rating["acceptability"], "neither")

    def test_autoplays_a_then_b_and_tracks_controls(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.start_auto_playback()
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertEqual(dialog.completed_sides, {"a"})
            self.application.processEvents()
            self.assertEqual(dialog.active_side, "b")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertEqual(dialog.completed_sides, {"a", "b"})
            self.assertTrue(dialog.tie.isEnabled())
            dialog.toggle_playback()
            self.assertEqual(len(dialog.playback.play_calls), 3)
            self.assertEqual(dialog.stop.text(), "Stop")
            dialog.deleteLater()

    def test_seek_skip_and_track_click(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.active_side = "a"
            clip = PcmClip(np.empty((1_920_000, 1), dtype=np.float32), 16_000)
            dialog.audio_clips["a"] = clip
            dialog.playback.clip = clip
            dialog.seek.setRange(0, clip.duration_ms)
            dialog.seek_to(90_000)
            dialog.skip_by(5_000)
            self.assertEqual(dialog.time.text(), "1:35 / 2:00")
            dialog.show()
            self.application.processEvents()
            QTest.mouseClick(
                dialog.seek,
                Qt.MouseButton.LeftButton,
                pos=QPoint(dialog.seek.width() * 3 // 4, dialog.seek.height() // 2),
            )
            self.assertAlmostEqual(dialog.seek.value(), 90_000, delta=2_000)
            dialog.deleteLater()

    def test_rapid_switch_reuses_one_persistent_playback_stream(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            playback = dialog.playback

            for side in ("a", "b", "a"):
                dialog.play(side)

            self.assertIs(dialog.playback, playback)
            self.assertEqual(len(playback.play_calls), 3)
            self.assertIs(playback.play_calls[0][0], dialog.audio_clips["a"])
            self.assertIs(playback.play_calls[1][0], dialog.audio_clips["b"])
            dialog.deleteLater()

    def test_complete_pcm_playback_finishes_timeline_and_underflow_does_not_count(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.play("b")
            dialog.playback.finish(underflowed=True)
            dialog.poll_playback()
            self.assertNotIn("b", dialog.completed_sides)
            self.assertIn("underflowed", dialog.status.text())

            dialog.play("b")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertIn("b", dialog.completed_sides)
            self.assertEqual(dialog.seek.value(), dialog.seek.maximum())
            dialog.deleteLater()

    def test_report_failure_advances_from_the_persisted_score(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            dialog.completed_sides = {"a", "b"}
            report_path = session.with_name("report.json").resolve()
            from vntts.authoring import listening as listening_module

            original_write = listening_module.atomic_write_json

            def fail_report(path, value, **kwargs):
                if Path(path).resolve() == report_path:
                    raise OSError("synthetic report failure")
                return original_write(path, value, **kwargs)

            with patch.object(
                listening_module, "atomic_write_json", side_effect=fail_report
            ):
                dialog.save_preference("a")
                self.wait_for(lambda: not dialog._preference_active)

            self.assertEqual(load_listening_session(session)["completed_count"], 1)
            self.assertIsNone(dialog.current_trial)
            self.assertIn("Preference was saved", dialog.status.text())
            self.assertFalse(dialog.prefer_a.isEnabled())
            dialog.deleteLater()

    def test_slow_save_keeps_qt_responsive_and_defers_close(self):
        with TemporaryDirectory() as directory:
            started = Event()
            release = Event()

            def slow_recorder(*args, **kwargs):
                started.set()
                release.wait(3)
                return record_trial_preference(*args, **kwargs)

            session, dialog = self.create_dialog(
                Path(directory), preference_recorder=slow_recorder
            )
            dialog.completed_sides = {"a", "b"}
            dialog.set_preference_buttons_enabled(True)
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))

            before = time.monotonic()
            dialog.save_preference("a")
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started.is_set() and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog._preference_active)
            self.assertFalse(dialog.prefer_a.isEnabled())
            self.assertTrue(dialog.play_a.isEnabled())
            self.assertIn("Saving preference", dialog.status.text())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn("Close is deferred", dialog.status.text())

            release.set()
            self.wait_for(lambda: not dialog._preference_active)
            self.assertEqual(load_listening_session(session)["completed_count"], 1)

    def test_transient_save_failure_can_retry_in_place(self):
        with TemporaryDirectory() as directory:
            attempts = 0

            def flaky_recorder(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("temporary disk failure")
                return record_trial_preference(*args, **kwargs)

            session, dialog = self.create_dialog(
                Path(directory), preference_recorder=flaky_recorder
            )
            dialog.completed_sides = {"a", "b"}
            dialog.set_preference_buttons_enabled(True)

            dialog.save_preference("a")
            self.wait_for(lambda: not dialog._preference_active)
            self.assertIn("Choose again to retry", dialog.status.text())
            self.assertTrue(dialog.prefer_a.isEnabled())
            self.assertEqual(load_listening_session(session)["completed_count"], 0)

            dialog.save_preference("a")
            self.wait_for(lambda: not dialog._preference_active)
            self.assertEqual(load_listening_session(session)["completed_count"], 1)
            self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
