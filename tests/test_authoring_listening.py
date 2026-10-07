import json
import os
import struct
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

import vntts.authoring.listening as listening_module
from tests.listening_fixtures import FakePlayback, write_model_reports
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
from vntts.authoring.pcm_playback import PcmClip

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


class AuthoringListeningTest(unittest.TestCase):
    def test_loader_rejects_malformed_trial_identity_and_enum_fields(self):
        mutations = (
            ("trial_id", Ellipsis),
            ("queue_id", Ellipsis),
            ("trial_id", None),
            ("trial_id", []),
            ("trial_id", {}),
            ("trial_id", 1),
            ("queue_id", None),
            ("queue_id", []),
            ("rating", {"preference": []}),
            ("rating", {"preference": "tie", "acceptability": {}}),
        )
        for legacy in (False, True):
            for field, value in mutations:
                with (
                    self.subTest(legacy=legacy, field=field, value=value),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    path = (
                        (write_listening_fixture(root) / "session.json")
                        if legacy
                        else create_listening_session_from_reports(
                            write_model_reports(root, item_count=1), root / "session"
                        )
                    )
                    session = json.loads(path.read_text())
                    trial = session["trials"][0]
                    trial["line_id"] = "line-0"
                    if value is Ellipsis:
                        trial.pop(field, None)
                    else:
                        trial[field] = value
                    if field == "rating":
                        session["completed_count"] = sum(
                            item.get("rating") is not None for item in session["trials"]
                        )
                    path.write_text(json.dumps(session), encoding="utf-8")
                    before = path.read_bytes()
                    with self.assertRaises(ModelListeningError):
                        load_listening_session(path)
                    self.assertEqual(path.read_bytes(), before)

    def test_unrated_legacy_trial_can_omit_rating_and_text(self):
        with TemporaryDirectory() as directory:
            path = write_listening_fixture(Path(directory)) / "session.json"
            document = json.loads(path.read_text())
            for trial in document["trials"]:
                for field in ("rating", "line_id", "text", "text_sha256"):
                    trial.pop(field, None)
            document["completed_count"] = 0
            path.write_text(json.dumps(document), encoding="utf-8")
            before = path.read_bytes()
            loaded = load_listening_session(path)
            self.assertEqual(listening_progress(loaded), (0, len(document["trials"])))
            self.assertIsNotNone(next_pending_trial(loaded))
            self.assertEqual(path.read_bytes(), before)

    def test_current_session_rejects_non_list_dimensions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session"
            )
            document = json.loads(path.read_text())
            document["dimensions"] = {"invalid": "container"}
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ModelListeningError):
                load_listening_session(path)

    def test_malformed_report_schema_and_outcome_are_domain_errors(self):
        for field in ("schema", "outcome"):
            for value in ([], {}):
                with (
                    self.subTest(field=field, value=value),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    reports = write_model_reports(root, item_count=1)
                    document = json.loads(reports[0].read_text())
                    target = document["samples"][0] if field == "outcome" else document
                    target[field] = value
                    reports[0].write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ModelListeningError):
                        create_listening_session_from_reports(reports, root / "session")
                    self.assertFalse((root / "session").exists())

    def test_malformed_model_labels_are_domain_errors(self):
        for field in ("provider", "model"):
            for value in ([], {}):
                with (
                    self.subTest(field=field, value=value),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    reports = write_model_reports(root, item_count=1)
                    document = json.loads(reports[0].read_text())
                    target = document
                    target[field] = value
                    reports[0].write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ModelListeningError):
                        create_listening_session_from_reports(reports, root / "session")
                    self.assertFalse((root / "session").exists())

    def test_hidden_key_and_report_keep_explicit_model_metadata(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), TemporaryDirectory() as directory:
                root = Path(directory)
                reports = write_model_reports(root, item_count=1)
                expected = {}
                for index, path in enumerate(reports):
                    document = json.loads(path.read_text())
                    if explicit:
                        document.update(
                            provider=f"provider-{index}", model=f"model-{index}"
                        )
                    else:
                        document.pop("provider")
                        document.pop("model")
                    path.write_text(json.dumps(document), encoding="utf-8")
                    expected[document["model_id"]] = (
                        document.get("provider", document["backend"]),
                        document.get("model", document["model_id"]),
                    )
                session = create_listening_session_from_reports(
                    reports, root / "session"
                )
                key = json.loads(session.with_name(".blind-key.json").read_text())
                report = aggregate_listening_report(session)
                for models in (key["models"], report["models"]):
                    self.assertEqual(
                        {
                            item["model_id"]: (item["provider"], item["model"])
                            for item in models
                        },
                        expected,
                    )
                self.assertNotIn("provider-", session.read_text())

    def test_blind_assignment_matching_grows_linearly(self):
        class CountedId(str):
            comparisons = 0
            __hash__ = str.__hash__

            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = create_listening_session_from_reports(
                write_model_reports(root, item_count=16), root / "session"
            )
            expected = load_listening_session(path)
            read_json = listening_module._load_json

            def count_ids(path, description):
                value = read_json(path, description)
                if description == "listening session":
                    for trial in value["trials"]:
                        trial["trial_id"] = CountedId(trial["trial_id"])
                return value

            with patch.object(listening_module, "_load_json", side_effect=count_ids):
                actual = load_listening_session(path)
            comparisons = CountedId.comparisons
            self.assertEqual(actual, expected)
            self.assertLessEqual(comparisons, 4 * len(expected["trials"]))

    def test_session_creation_failure_leaves_destination_retryable(self):
        for existing_empty in (False, True):
            with (
                self.subTest(existing_empty=existing_empty),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                reports = write_model_reports(root, item_count=1)
                destination = root / "session"
                if existing_empty:
                    destination.mkdir()
                verify = listening_module._verify_pcm_audio
                aliases = []

                def fail_alias(path, digest, label):
                    verify(path, digest, label)
                    if label == "blind audio alias":
                        aliases.append(path)
                        if len(aliases) == 2:
                            raise ModelListeningError(
                                "injected alias validation failure"
                            )

                with patch.object(
                    listening_module, "_verify_pcm_audio", side_effect=fail_alias
                ):
                    with self.assertRaisesRegex(ModelListeningError, "injected alias"):
                        create_listening_session_from_reports(reports, destination)
                self.assertEqual(destination.exists(), existing_empty)
                if existing_empty:
                    self.assertEqual(list(destination.iterdir()), [])
                self.assertEqual(list(root.glob(".session-*")), [])
                path = create_listening_session_from_reports(reports, destination)
                self.assertEqual(len(load_listening_session(path)["trials"]), 1)

    def test_session_creation_rejects_changed_report_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=1)
            link = listening_module._link_blind_audio

            def change_report(source, destination):
                link(source, destination)
                document = json.loads(reports[0].read_text())
                document["model"] = "changed model"
                reports[0].write_text(json.dumps(document), encoding="utf-8")

            with patch.object(
                listening_module, "_link_blind_audio", side_effect=change_report
            ):
                with self.assertRaisesRegex(ModelListeningError, "report changed"):
                    create_listening_session_from_reports(reports, root / "session")
            self.assertFalse((root / "session").exists())
            self.assertEqual(list(root.glob(".session-*")), [])

    def test_concurrent_session_creators_preserve_one_complete_winner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=1)
            barrier = Barrier(2)
            link = listening_module._link_blind_audio
            results, errors = [], []

            def overlap(source, destination):
                if destination.name.endswith("-a.wav"):
                    barrier.wait(timeout=10)
                link(source, destination)

            def create(seed):
                try:
                    results.append(
                        create_listening_session_from_reports(
                            reports, root / "session", seed=seed
                        )
                    )
                except Exception as error:
                    errors.append(error)

            with patch.object(
                listening_module, "_link_blind_audio", side_effect=overlap
            ):
                threads = [Thread(target=create, args=(seed,)) for seed in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ModelListeningError)
            self.assertEqual(len(load_listening_session(results[0])["trials"]), 1)
            self.assertEqual(list(root.glob(".session-*")), [])

    def test_public_creation_and_loading_reject_truncated_wav_payloads(self):
        for loading, missing_padding in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with (
                self.subTest(loading=loading, missing_padding=missing_padding),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                reports = write_model_reports(root, item_count=1)
                if loading:
                    session_path = create_listening_session_from_reports(
                        reports, root / "session"
                    )
                    session = json.loads(session_path.read_text())
                    audio = session_path.parent / session["trials"][0]["audio"]["a"]
                else:
                    document = json.loads(reports[0].read_text())
                    audio = Path(document["samples"][0]["audio"])
                payload = audio.read_bytes()
                if missing_padding:
                    payload += b"JUNK" + struct.pack("<I", 17) + b"x" * 17
                    payload = (
                        payload[:4] + struct.pack("<I", len(payload) - 8) + payload[8:]
                    )
                else:
                    payload = payload[:-100]
                audio.write_bytes(payload)
                if not loading:
                    document["samples"][0]["audio_sha256"] = sha256_file(audio)
                    reports[0].write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ModelListeningError, "not a supported WAV"):
                    if loading:
                        load_listening_session(session_path)
                    else:
                        create_listening_session_from_reports(reports, root / "session")
                if not loading:
                    self.assertFalse((root / "session").exists())

    def test_public_creation_and_loading_use_bounded_wav_probe_reads(self):
        class ProbeStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size):
                if size > 16:
                    raise AssertionError("WAV envelope probe read the full audio chunk")
                return self.stream.read(size)

            def seek(self, *args):
                return self.stream.seek(*args)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports = write_model_reports(root, item_count=1)
            probe = listening_module._probe_supported_wav
            open_path = Path.open

            def bounded_probe(path):
                with patch.object(
                    Path,
                    "open",
                    lambda path, *args, **kwargs: ProbeStream(
                        open_path(path, *args, **kwargs)
                    ),
                ):
                    return probe(path)

            with patch.object(
                listening_module, "_probe_supported_wav", side_effect=bounded_probe
            ):
                path = create_listening_session_from_reports(reports, root / "session")
                self.assertEqual(len(load_listening_session(path)["trials"]), 1)

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

    def test_aggregate_parses_only_session_bound_key_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session"
            )
            key_path = session_path.with_name(".blind-key.json")
            original = key_path.read_bytes()
            changed = json.loads(original)
            changed["models"][0]["provider"] = "unbound-provider"
            replacement = json.dumps(changed).encode()
            original_hash = listening_module.sha256_file
            original_capture = listening_module.capture_authority_file

            def replace_after_hash(path):
                if Path(path) != key_path:
                    return original_hash(path)
                key_path.write_bytes(original)
                digest = original_hash(path)
                key_path.write_bytes(replacement)
                return digest

            def replace_after_capture(path, label, **kwargs):
                if Path(path) == key_path:
                    key_path.write_bytes(original)
                snapshot = original_capture(path, label, **kwargs)
                if Path(path) == key_path:
                    key_path.write_bytes(replacement)
                return snapshot

            with (
                patch.object(
                    listening_module, "sha256_file", side_effect=replace_after_hash
                ),
                patch.object(
                    listening_module,
                    "capture_authority_file",
                    side_effect=replace_after_capture,
                ),
            ):
                report = aggregate_listening_report(session_path)

            self.assertEqual(key_path.read_bytes(), replacement)
            self.assertEqual(
                {model["provider"] for model in report["models"]}, {"synthetic"}
            )

    def test_aggregate_accepts_matching_captured_session_and_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session", seed=5
            )
            session = load_listening_session(session_path)
            key = json.loads(
                session_path.with_name(".blind-key.json").read_text(encoding="utf-8")
            )
            captured = aggregate_listening_report(session_path)
            report = aggregate_listening_report(
                session_path,
                session_path.with_name("captured-report.json"),
                expected_session=session,
                expected_key=key,
            )

        captured.pop("generated_at")
        report.pop("generated_at")
        self.assertEqual(report, captured)

    def test_aggregate_rejects_mismatched_captured_documents_without_overwrite(self):
        for mismatch in ("session", "key"):
            with self.subTest(mismatch=mismatch), TemporaryDirectory() as directory:
                root = Path(directory)
                session_path = create_listening_session_from_reports(
                    write_model_reports(root, item_count=1), root / "session", seed=5
                )
                session = load_listening_session(session_path)
                key = json.loads(
                    session_path.with_name(".blind-key.json").read_text(
                        encoding="utf-8"
                    )
                )
                expected_session = json.loads(json.dumps(session))
                expected_key = json.loads(json.dumps(key))
                if mismatch == "session":
                    expected_session["completed_count"] += 1
                else:
                    expected_key["assignments"][0]["a"]["model_id"] += "-changed"
                output = session_path.with_name("report.json")
                output.write_text("preserve this report", encoding="utf-8")

                with self.assertRaisesRegex(
                    ModelListeningError,
                    f"Listening {mismatch} changed before report aggregation",
                ):
                    aggregate_listening_report(
                        session_path,
                        output,
                        expected_session=expected_session,
                        expected_key=expected_key,
                    )

                self.assertEqual(
                    output.read_text(encoding="utf-8"), "preserve this report"
                )

    def test_aggregate_uses_exact_json_number_semantics_for_captured_documents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session", seed=5
            )
            session = load_listening_session(session_path)
            key = json.loads(
                session_path.with_name(".blind-key.json").read_text(encoding="utf-8")
            )
            expected_session = json.loads(json.dumps(session))
            expected_session["completed_count"] = bool(
                expected_session["completed_count"]
            )
            expected_key = json.loads(json.dumps(key))
            expected_key["schema_version"] = float(expected_key["schema_version"])

            self.assertEqual(expected_session, session)
            self.assertEqual(expected_key, key)
            for expected_session_value, expected_key_value, label in (
                (expected_session, key, "session"),
                (session, expected_key, "key"),
            ):
                with (
                    self.subTest(label=label),
                    self.assertRaisesRegex(
                        ModelListeningError,
                        f"Listening {label} changed before report aggregation",
                    ),
                ):
                    aggregate_listening_report(
                        session_path,
                        expected_session=expected_session_value,
                        expected_key=expected_key_value,
                    )

    def test_aggregate_rejects_non_finite_captured_documents_with_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=1), root / "session", seed=5
            )
            session = load_listening_session(session_path)
            key = json.loads(session_path.with_name(".blind-key.json").read_text())
            for expected in ("session", "key"):
                with self.subTest(expected=expected):
                    captured = dict(session if expected == "session" else key)
                    captured["invalid"] = float("nan")
                    with self.assertRaisesRegex(ModelListeningError, "not valid JSON"):
                        aggregate_listening_report(
                            session_path,
                            expected_session=captured
                            if expected == "session"
                            else session,
                            expected_key=captured if expected == "key" else key,
                        )

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
        mutations = (("alias", "checksum changed"),)
        if os.name != "nt":
            mutations += (("mode", "0600"),)
        for mutation, pattern in mutations:
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

    def test_imported_legacy_session_rejects_non_integer_versions_and_counts(self):
        mutations = (
            ("import.json", "schema_version", True),
            ("import.json", "schema_version", 1.0),
            ("session.json", "schema_version", True),
            ("session.json", "schema_version", 1.0),
            ("session.json", "completed_count", True),
            ("session.json", "completed_count", 1.0),
            ("session.json", "trial_count", True),
            ("session.json", "trial_count", 1.0),
        )
        for filename, field, value in mutations:
            with (
                self.subTest(filename=filename, field=field, value=value),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                imported = import_listening_session(
                    write_listening_fixture(root), root / "app-data"
                ).destination
                path = imported / filename
                document = json.loads(path.read_text(encoding="utf-8"))
                document[field] = value
                path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

                with self.assertRaises(ModelListeningError):
                    load_listening_session(imported / "session.json")

    def test_imported_legacy_report_rejects_boolean_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            imported = import_listening_session(
                write_listening_fixture(root), root / "app-data"
            ).destination
            path = imported / "report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["completed_trials"] = True
            path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")

            regenerated = ensure_listening_report(imported / "session.json")

        self.assertIs(type(regenerated["completed_trials"]), int)

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

    def test_concurrent_preferences_preserve_both_trials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session_path = create_listening_session_from_reports(
                write_model_reports(root, item_count=2), root / "session"
            )
            trial_ids = [
                trial["trial_id"]
                for trial in load_listening_session(session_path)["trials"]
            ]
            from vntts.authoring import listening as listening_module

            original_load = listening_module._load_listening_documents
            first_loaded = Event()
            release_first = Event()
            calls_lock = Lock()
            calls = 0

            def coordinated_load(path):
                nonlocal calls
                session = original_load(path)
                with calls_lock:
                    calls += 1
                    call_number = calls
                if call_number == 1:
                    first_loaded.set()
                    release_first.wait(2)
                return session

            errors = []

            def record(trial_id, preference):
                try:
                    record_trial_preference(session_path, trial_id, preference)
                except Exception as error:
                    errors.append(error)

            with patch.object(
                listening_module,
                "_load_listening_documents",
                side_effect=coordinated_load,
            ):
                first = Thread(target=record, args=(trial_ids[0], "a"))
                second = Thread(target=record, args=(trial_ids[1], "b"))
                first.start()
                self.assertTrue(first_loaded.wait(1))
                second.start()
                time.sleep(0.05)
                release_first.set()
                first.join(2)
                second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            saved = load_listening_session(session_path)
            self.assertEqual(saved["completed_count"], 2)
            self.assertTrue(
                all(trial["rating"] is not None for trial in saved["trials"])
            )


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
        kwargs.setdefault("confirmer", lambda _preference: True)
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
            dialog.show()
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
            self.assertTrue(dialog.play_a.isHidden())
            self.assertTrue(dialog.prefer_a.isHidden())
            self.assertFalse(dialog.open_report.isHidden())
            self.assertTrue(dialog.open_report.hasFocus())
            with patch(
                "vntts.authoring.listening_ui.QDesktopServices.openUrl",
                return_value=True,
            ) as open_url:
                dialog.open_report.click()
            self.assertEqual(
                open_url.call_args.args[0].toLocalFile(),
                str(session.with_name("report.json")),
            )
            dialog.deleteLater()

    def test_trial_hierarchy_keyboard_accessibility_and_compact_layout(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory), item_count=3)
            dialog.show()
            dialog.resize(640, 400)
            dialog.activateWindow()
            self.application.processEvents()

            self.assertEqual(dialog.progress.text(), "Completed 0 of 3 | Remaining 3")
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
                dialog.context_toggle,
                dialog.review_scroll,
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

    def test_enlarged_text_keeps_dialogue_playback_and_decisions_reachable(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            font = dialog.font()
            font.setPointSize(16)
            dialog.setFont(font)
            dialog.resize(640, 400)
            dialog.show()
            self.application.processEvents()

            self.assertTrue(dialog.dialogue.isVisibleTo(dialog))
            self.assertTrue(dialog.play_a.isVisibleTo(dialog))
            self.assertTrue(dialog.play_b.isVisibleTo(dialog))
            self.assertLessEqual(
                dialog.play_a.mapTo(dialog.review_scroll.viewport(), QPoint(0, 0)).y()
                + dialog.play_a.height(),
                dialog.review_scroll.viewport().height(),
            )
            self.assertGreaterEqual(
                dialog.neither.height(), dialog.neither.sizeHint().height()
            )
            dialog.dialogue.setPlainText("Long blind dialogue. " * 80)
            self.assertGreater(dialog.dialogue.verticalScrollBar().maximum(), 0)
            dialog.context_toggle.setFocus()
            self.application.processEvents()
            toggle_top = dialog.context_toggle.mapTo(
                dialog.review_scroll.viewport(), QPoint(0, 0)
            ).y()
            self.assertGreaterEqual(toggle_top, 0)
            self.assertLessEqual(
                toggle_top + dialog.context_toggle.height(),
                dialog.review_scroll.viewport().height(),
            )
            dialog.context_toggle.setChecked(True)
            self.assertTrue(dialog.decision_context.isVisibleTo(dialog))
            large_font = dialog.font()
            large_font.setPixelSize(48)
            dialog.setFont(large_font)
            self.assertGreaterEqual(
                dialog.dialogue.viewport().height(), dialog.fontMetrics().height()
            )
            dialog.close()

    def test_complete_report_stays_visible_with_large_text(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            record_trial_preference(session, dialog.current_trial["trial_id"], "a")
            font = dialog.font()
            font.setPixelSize(48)
            dialog.setFont(font)
            dialog.load_next_trial()
            dialog.show()
            self.application.processEvents()

            viewport = dialog.review_scroll.viewport()
            status_bottom = dialog.status.mapTo(viewport, QPoint(0, 0)).y()
            status_bottom += dialog.status.height()
            report_bottom = dialog.open_report.mapTo(viewport, QPoint(0, 0)).y()
            report_bottom += dialog.open_report.height()
            self.assertLessEqual(status_bottom, viewport.height())
            self.assertLessEqual(report_bottom, viewport.height())
            dialog.close()

    def test_neither_acceptable_button_persists_distinct_verdict(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            dialog.completed_sides = {"a", "b"}
            dialog.set_preference_buttons_enabled(True)
            dialog.neither.click()
            self.wait_for(lambda: not dialog._preference_active)

            rating = load_listening_session(session)["trials"][0]["rating"]
            self.assertIn("No preference leader", dialog.status.text())

        self.assertEqual(rating["preference"], "tie")
        self.assertEqual(rating["acceptability"], "neither")

    def test_equal_preference_result_does_not_claim_single_leader(self):
        with TemporaryDirectory() as directory:
            session, dialog = self.create_dialog(Path(directory))
            record_trial_preference(session, dialog.current_trial["trial_id"], "tie")
            dialog.load_next_trial()

            self.assertIn("Top preference is tied", dialog.status.text())
            dialog.close()

    def test_irreversible_preference_can_be_cancelled(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(
                Path(directory), confirmer=lambda _preference: False
            )
            dialog.completed_sides = {"a", "b"}

            dialog.save_preference("a")

            self.assertFalse(dialog._preference_active)
            self.assertIn("cancelled", dialog.status.text())
            dialog.deleteLater()

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
            self.assertEqual(dialog.stop.text(), "Pause")
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

    def test_decision_requires_uninterrupted_initial_playback_not_seek_to_end(self):
        with TemporaryDirectory() as directory:
            _session, dialog = self.create_dialog(Path(directory))
            dialog.play("a")
            dialog.playback.started = True
            dialog.playback.position = dialog.playback.clip.frames // 2
            dialog.poll_playback()

            self.assertNotIn("a", dialog.completed_sides)
            self.assertFalse(dialog.prefer_a.isEnabled())

            dialog.seek_to(dialog.audio_clips["a"].duration_ms)
            dialog.playback.finish()
            dialog.poll_playback()

            self.assertNotIn("a", dialog.completed_sides)
            self.assertFalse(dialog.prefer_a.isEnabled())
            self.assertIn("without seeking", dialog.decision_reason.text())

            dialog.play("a")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertEqual(dialog.completed_sides, {"a"})
            self.assertIn("sample B", dialog.decision_reason.text())

            dialog.play("b")
            dialog.playback.finish()
            dialog.poll_playback()
            self.assertTrue(dialog.prefer_a.isEnabled())

            dialog.seek_to(0)
            dialog.play("a")
            self.assertTrue(dialog.seek.isEnabled())
            self.assertTrue(dialog.prefer_a.isEnabled())
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
            dialog.reject()
            self.assertTrue(dialog._close_pending)
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
