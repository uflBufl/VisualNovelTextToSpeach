import hashlib
import io
import json
import unittest
import wave
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring import robustness_corpus
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    build_cohort_review_decision,
    build_cohort_review_plan,
    write_cohort_review_decision,
)
from vntts.authoring.robustness_asr import (
    SpeechRobustnessAsrError,
    _WhisperTranscriber,
    build_speech_robustness_asr_report,
    compare_speech_transcript,
    write_speech_robustness_asr_report,
)
from vntts.authoring.robustness_corpus import (
    SpeechRobustnessCorpusError,
    analyze_speech_robustness_bytes,
    load_speech_robustness_corpus,
    publish_speech_robustness_corpus,
)


def _pending_workspace(root):
    _fixture, _imported, created = create_test_workspace(root)
    state_path = created.directory / "generated-audio/generation-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    queue_id, item = next(iter(state["items"].items()))
    item.update(
        {
            "status": "generated",
            "review_status": "pending_review",
            "generation_profile": "stable",
            "voice_character": "Rhiannon",
            "prompt_applied": False,
            "synthesis_provenance_sha256": "b" * 64,
        }
    )
    state["active"] = None
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return created.directory, state_path, queue_id


def _decision(workspace, queue_id, assessment="acceptable", defect_reasons=None):
    plan = build_cohort_review_plan(workspace)
    cohort_id = plan.document["cohorts"][0]["cohort_id"]
    decision = build_cohort_review_decision(
        plan,
        cohort_id,
        "rejected" if assessment == "bad" else "accepted",
        reviewed_queue_ids=[queue_id],
        sample_assessments={
            queue_id: (
                assessment
                if defect_reasons is None
                else {
                    "assessment": assessment,
                    "defect_reasons": defect_reasons,
                }
            )
        },
    )
    path = workspace / "cohort-reviews" / f"decision-{decision.decision_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_cohort_review_decision(decision, path)
    return path


def _wav_bytes(samples, rate=16_000):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return output.getvalue()


def _canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class AuthoringRobustnessCorpusTest(unittest.TestCase):
    def test_public_reader_rejects_boolean_schema_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            result = publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], root / "corpus"
            )
            corpus_path = result.directory / "corpus.json"
            document = json.loads(corpus_path.read_text(encoding="utf-8"))
            document["schema_version"] = True
            document["corpus_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "corpus_id"}
            )
            corpus_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(SpeechRobustnessCorpusError, "unsupported"):
                load_speech_robustness_corpus(result.directory)

    def test_version_three_preserves_exact_human_defect_reasons(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(
                workspace,
                queue_id,
                assessment="bad",
                defect_reasons=["timbre_or_audio_artifact", "pause_or_pacing"],
            )

            result = publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], root / "corpus"
            )
            sample = load_speech_robustness_corpus(result.directory).document[
                "samples"
            ][0]

        self.assertEqual(
            sample["human_defect_reasons"],
            ["pause_or_pacing", "timbre_or_audio_artifact"],
        )

    def test_word_comparison_reports_insertions_deletions_and_substitutions(self):
        comparison = compare_speech_transcript(
            "The barrier begins to crack", "The barrier begins and cracks again"
        )

        self.assertEqual(comparison["expected_word_count"], 5)
        self.assertGreater(comparison["distance"], 0)
        self.assertGreater(comparison["insertions"], 0)

    def test_word_comparison_preserves_tie_breaking_and_normalization(self):
        for expected, observed, edits in (
            ("", "one two", (2, 0, 2, 0)),
            ("one two", "", (2, 0, 0, 2)),
            ("one two", "two one", (2, 0, 1, 1)),
            ("one one", "one", (1, 0, 0, 1)),
            ("one two one", "two one two", (2, 0, 1, 1)),
            ("One, isn’t two.", "one ISN'T three", (1, 1, 0, 0)),
        ):
            with self.subTest(expected=expected, observed=observed):
                comparison = compare_speech_transcript(expected, observed)
                self.assertEqual(
                    tuple(
                        comparison[field]
                        for field in (
                            "distance",
                            "substitutions",
                            "insertions",
                            "deletions",
                        )
                    ),
                    edits,
                )

    def test_whisper_input_rejects_incomplete_pcm_before_resampling(self):
        for payload in (
            _wav_bytes([]),
            _wav_bytes([1, 2, 3, 4])[:-2],
            _wav_bytes([1, 2, 3, 4])[:24] + bytes(4) + _wav_bytes([1, 2, 3, 4])[28:],
        ):
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(SpeechRobustnessAsrError):
                    _WhisperTranscriber._input(payload)

    def test_asr_resume_preserves_sample_metadata_and_immutable_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            corpus = root / "corpus"
            publish_speech_robustness_corpus([workspace / "cohort-reviews"], [], corpus)
            model = root / "model"
            model.mkdir()
            weights = model / "weights.bin"
            weights.write_bytes(b"model")
            progress = root / "progress.json"
            build_speech_robustness_asr_report(
                corpus, model, transcriber=lambda _: "text", progress_path=progress
            )
            original = progress.read_bytes()
            for field, value in (("human_label", "bad"), ("provider", "different")):
                with self.subTest(field=field):
                    document = json.loads(original)
                    document["records"][0][field] = value
                    document["progress_id"] = _canonical_sha256(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "progress_id"
                        }
                    )
                    progress.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(
                        SpeechRobustnessAsrError, "record authority"
                    ):
                        build_speech_robustness_asr_report(
                            corpus,
                            model,
                            transcriber=lambda _: self.fail("changed progress resumed"),
                            progress_path=progress,
                        )
            for version in (2.0, float("nan")):
                with self.subTest(version=version):
                    document = json.loads(original)
                    document["schema_version"] = version
                    document["progress_id"] = _canonical_sha256(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "progress_id"
                        }
                    )
                    progress.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(SpeechRobustnessAsrError):
                        build_speech_robustness_asr_report(
                            corpus,
                            model,
                            transcriber=lambda _: self.fail("invalid progress resumed"),
                            progress_path=progress,
                        )
            with self.assertRaisesRegex(SpeechRobustnessAsrError, "immutable model"):
                build_speech_robustness_asr_report(
                    corpus,
                    model,
                    transcriber=lambda _: self.fail("model modified"),
                    progress_path=model / "progress.json",
                )
            self.assertEqual(weights.read_bytes(), b"model")
            self.assertEqual(
                sorted(path.name for path in model.iterdir()), ["weights.bin"]
            )

    def test_asr_report_rejects_inexact_policy_version_and_nonfinite_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            corpus = root / "corpus"
            publish_speech_robustness_corpus([workspace / "cohort-reviews"], [], corpus)
            model = root / "model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"model")
            report = build_speech_robustness_asr_report(
                corpus, model, transcriber=lambda _: "text"
            )
            for field, value in (
                ("schema_version", 2.0),
                ("corpus_schema_version", 3.0),
                ("policy", {"diagnostic_only": 1, "automatic_rejection": 0}),
                ("asr", {"invalid": float("nan")}),
            ):
                with self.subTest(field=field):
                    document = report.to_dict()
                    document[field] = value
                    document["report_id"] = _canonical_sha256(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "report_id"
                        }
                    )
                    output = root / "invalid.json"
                    with self.assertRaises(SpeechRobustnessAsrError):
                        write_speech_robustness_asr_report(
                            replace(report, document=document), output
                        )
                    self.assertFalse(output.exists())

    def test_asr_batch_results_match_single_results_and_reject_a_string(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            corpus = root / "corpus"
            publish_speech_robustness_corpus([workspace / "cohort-reviews"], [], corpus)
            model = root / "model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"model")
            single = build_speech_robustness_asr_report(
                corpus, model, transcriber=lambda _: "text"
            )

            class Batch:
                def __init__(self, result):
                    self.result = result

                def __call__(self, payload):
                    raise AssertionError("batch fallback")

                def transcribe_many(self, payloads):
                    return self.result

            batch = build_speech_robustness_asr_report(
                corpus, model, transcriber=Batch(["text"])
            )
            self.assertEqual(batch.document, single.document)
            for result in ("x", None, [], [1]):
                with self.subTest(result=result):
                    with self.assertRaisesRegex(SpeechRobustnessAsrError, "batch text"):
                        build_speech_robustness_asr_report(
                            corpus, model, transcriber=Batch(result)
                        )

    def test_audio_reports_reuse_one_decode_and_preserve_metrics(self):
        from dataclasses import asdict

        from vntts.authoring.speech_quality import measure_generated_speech_bytes

        for count in (1, 1_280, 1_281):
            samples = np.full(count, 184, dtype=np.int16)
            samples[count // 3 : count // 2] = 0
            payload = _wav_bytes(samples)
            expected = asdict(measure_generated_speech_bytes(payload))
            with patch("wave.open", wraps=wave.open) as opened:
                report = analyze_speech_robustness_bytes(payload)
                self.assertEqual(opened.call_count, 1)
            self.assertEqual(report["speech_quality"], expected)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            with patch.object(
                robustness_corpus, "_read_pcm16", wraps=robustness_corpus._read_pcm16
            ) as decoded:
                corpus = publish_speech_robustness_corpus(
                    [workspace / "cohort-reviews"], [], root / "corpus"
                )
            # One decode for creation, one for staged and one for final validation.
            self.assertEqual(decoded.call_count, 3)
            with patch.object(
                robustness_corpus, "_read_pcm16", wraps=robustness_corpus._read_pcm16
            ) as decoded:
                loaded = load_speech_robustness_corpus(corpus.directory)
            self.assertEqual(decoded.call_count, 1)
            self.assertEqual(loaded.corpus_id, corpus.corpus_id)

    def test_exact_active_pcm_repetition_is_diagnostic_only(self):
        rng = np.random.default_rng(42)
        segment = rng.integers(-8_000, 8_000, size=12 * 320, dtype=np.int16)
        analysis = analyze_speech_robustness_bytes(
            _wav_bytes(np.concatenate((segment, segment)))
        )

        self.assertIn("exact_pcm_repeat_candidate", analysis["signals"])
        self.assertGreaterEqual(analysis["exact_active_repeat"]["seconds"], 0.24)
        self.assertTrue(analysis["policy"]["diagnostic_only"])
        self.assertFalse(analysis["policy"]["automatic_rejection"])

    def test_publication_is_lossless_idempotent_and_fully_validated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state_path, queue_id = _pending_workspace(root / "accepted")
            decision_path = _decision(workspace, queue_id)
            source_state = state_path.read_bytes()
            source_decision = decision_path.read_bytes()
            output = root / "corpus"

            first = publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], output
            )
            second = publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], output
            )
            loaded = load_speech_robustness_corpus(output)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.corpus_id, second.corpus_id)
            self.assertEqual(loaded.sample_count, 1)
            self.assertEqual(loaded.failure_count, 0)
            self.assertEqual(
                loaded.document["summary"]["human_labels"], {"acceptable": 1}
            )
            self.assertEqual(state_path.read_bytes(), source_state)
            self.assertEqual(decision_path.read_bytes(), source_decision)
            sample = loaded.document["samples"][0]
            self.assertEqual(loaded.document["schema_version"], 3)
            self.assertEqual(sample["queue_id"], queue_id)
            self.assertEqual(sample["human_label"], "acceptable")
            self.assertEqual(sample["human_defect_reasons"], [])
            self.assertEqual(
                sample["decision_ids"], [json.loads(source_decision)["decision_id"]]
            )

            audio = output / sample["audio"]
            audio.write_bytes(audio.read_bytes() + b"tampered")
            with self.assertRaisesRegex(
                SpeechRobustnessCorpusError, "artifact changed"
            ):
                load_speech_robustness_corpus(output)

    def test_failed_state_records_are_preserved_without_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(reviewed, queue_id, assessment="bad")
            failed, failed_state_path, failed_queue_id = _pending_workspace(
                root / "failed"
            )
            state = json.loads(failed_state_path.read_text(encoding="utf-8"))
            state["items"][failed_queue_id] = {
                "status": "failed",
                "attempts": 4,
                "seed": 3,
                "last_error": "MOSS generation hit the text-length audio limit before EOS",
                "provider": "moss-tts",
                "model": "moss-local",
                "generation_profile": "stable",
                "updated_at": "2026-08-27T00:00:00+00:00",
            }
            failed_state_path.write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )

            result = publish_speech_robustness_corpus(
                [reviewed / "cohort-reviews"], [failed], root / "corpus"
            )
            loaded = load_speech_robustness_corpus(result.directory)

        self.assertEqual(loaded.sample_count, 1)
        self.assertEqual(loaded.failure_count, 1)
        self.assertEqual(loaded.document["samples"][0]["human_label"], "bad")
        self.assertEqual(
            loaded.document["failures"][0]["failure"]["kind"],
            "missed_eos_audio_limit",
        )

    def test_legacy_heard_only_decision_is_not_guessed_into_a_label(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            path = _decision(workspace, queue_id)
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("sample_assessments")
            document["decision_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "decision_id"}
            )
            legacy = path.with_name(f"decision-{document['decision_id']}.json")
            legacy.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            path.unlink()

            with self.assertRaisesRegex(
                SpeechRobustnessCorpusError, "No explicit acceptable/bad"
            ):
                publish_speech_robustness_corpus(
                    [workspace / "cohort-reviews"], [], root / "corpus"
                )

    def test_cli_publishes_and_checks_same_corpus(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            output = root / "corpus"
            published = StringIO()
            checked = StringIO()

            with redirect_stdout(published):
                publish_code = authoring_main(
                    [
                        "speech-robustness-corpus",
                        str(output),
                        "--decision-root",
                        str(workspace / "cohort-reviews"),
                    ]
                )
            with redirect_stdout(checked):
                check_code = authoring_main(["speech-robustness-check", str(output)])

        self.assertEqual(publish_code, 0)
        self.assertEqual(check_code, 0)
        self.assertEqual(json.loads(published.getvalue())["sample_count"], 1)
        self.assertEqual(json.loads(checked.getvalue())["sample_count"], 1)

    def test_whisper_input_preserves_duration_and_pitch_at_native_rate(self):
        for rate in (16_000, 24_000, 48_000):
            with self.subTest(rate=rate):
                samples = 8_000 * np.sin(2 * np.pi * 440 * np.arange(rate) / rate)
                result = _WhisperTranscriber._input(_wav_bytes(samples, rate))
                self.assertEqual(result["sampling_rate"], 16_000)
                self.assertEqual(len(result["array"]), 16_000)
                spectrum = np.abs(np.fft.rfft(result["array"]))
                self.assertEqual(int(spectrum.argmax()), 440)
                self.assertAlmostEqual(
                    float(np.max(result["array"])), 8_000 / 32768, places=2
                )

    def test_asr_report_is_model_and_corpus_bound_and_no_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
            _decision(workspace, queue_id)
            corpus = root / "corpus"
            publish_speech_robustness_corpus([workspace / "cohort-reviews"], [], corpus)
            model = root / "asr-model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"exact-model")
            progress = root / "asr-progress.json"

            report = build_speech_robustness_asr_report(
                corpus,
                model,
                transcriber=lambda _payload: "Earlier failure",
                progress_path=progress,
            )
            output = root / "asr-report.json"
            write_speech_robustness_asr_report(report, output)

            self.assertEqual(report.document["corpus_schema_version"], 3)
            self.assertEqual(report.document["summary"]["sample_count"], 1)
            self.assertTrue(report.document["policy"]["diagnostic_only"])
            self.assertGreater(
                report.document["records"][0]["comparison"]["word_error_rate"], 0
            )
            resumed = build_speech_robustness_asr_report(
                corpus,
                model,
                transcriber=lambda _payload: self.fail("completed sample reran"),
                progress_path=progress,
            )
            self.assertEqual(resumed.document, report.document)
            old_progress = json.loads(progress.read_text(encoding="utf-8"))
            old_progress["schema_version"] = 1
            old_progress["progress_id"] = _canonical_sha256(
                {
                    key: value
                    for key, value in old_progress.items()
                    if key != "progress_id"
                }
            )
            progress.write_text(json.dumps(old_progress), encoding="utf-8")
            with self.assertRaisesRegex(SpeechRobustnessAsrError, "resampling"):
                build_speech_robustness_asr_report(
                    corpus,
                    model,
                    progress_path=progress,
                    transcriber=lambda _payload: self.fail("stale progress resumed"),
                )
            with self.assertRaisesRegex(SpeechRobustnessAsrError, "output exists"):
                write_speech_robustness_asr_report(report, output)
            with self.assertRaisesRegex(
                SpeechRobustnessAsrError, "outside the immutable corpus"
            ):
                write_speech_robustness_asr_report(
                    report, corpus / "forbidden-report.json"
                )
            with self.assertRaisesRegex(
                SpeechRobustnessAsrError, "validated corpus authority"
            ):
                write_speech_robustness_asr_report(report.to_dict(), root / "raw.json")


if __name__ == "__main__":
    unittest.main()
