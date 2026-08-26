import hashlib
import io
import json
import unittest
import wave
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    build_cohort_review_decision,
    build_cohort_review_plan,
    write_cohort_review_decision,
)
from vntts.authoring.robustness_asr import (
    SpeechRobustnessAsrError,
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


def _decision(workspace, queue_id, assessment="acceptable"):
    plan = build_cohort_review_plan(workspace)
    cohort_id = plan.document["cohorts"][0]["cohort_id"]
    decision = build_cohort_review_decision(
        plan,
        cohort_id,
        "rejected" if assessment == "bad" else "accepted",
        reviewed_queue_ids=[queue_id],
        sample_assessments={queue_id: assessment},
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
    def test_word_comparison_reports_insertions_deletions_and_substitutions(self):
        comparison = compare_speech_transcript(
            "The barrier begins to crack", "The barrier begins and cracks again"
        )

        self.assertEqual(comparison["expected_word_count"], 5)
        self.assertGreater(comparison["distance"], 0)
        self.assertGreater(comparison["insertions"], 0)

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
            self.assertEqual(sample["queue_id"], queue_id)
            self.assertEqual(sample["human_label"], "acceptable")
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

            self.assertEqual(report.document["corpus_schema_version"], 2)
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
