import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.symlink_support import symlink_or_skip
from tests.test_authoring_robustness_corpus import _decision, _pending_workspace
from vntts.authoring.bulk_generation import sha256_control_path
from vntts.authoring.robustness_asr import (
    SpeechRobustnessAsrError,
    build_speech_robustness_asr_report,
    write_speech_robustness_asr_report,
)
from vntts.authoring.robustness_corpus import publish_speech_robustness_corpus


def _report(root, *, model=None):
    workspace, _state_path, queue_id = _pending_workspace(root / "reviewed")
    _decision(workspace, queue_id)
    publish_speech_robustness_corpus(
        [workspace / "cohort-reviews"], [], root / "corpus"
    )
    model_directory = root / "model"
    model_directory.mkdir()
    (model_directory / "weights.bin").write_bytes(b"model")
    return build_speech_robustness_asr_report(
        root / "corpus",
        model or model_directory,
        transcriber=lambda _: "text",
        progress_path=root / "progress.json",
    )


class SpeechRobustnessAsrPublicationTest(unittest.TestCase):
    def test_report_cannot_mutate_immutable_model_after_evaluation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = _report(root)
            expected_hash = sha256_control_path(report.model_directory)
            for output in (
                report.model_directory / "report.json",
                report.model_directory / "sub" / "report.json",
            ):
                with (
                    self.subTest(output=output),
                    self.assertRaisesRegex(
                        SpeechRobustnessAsrError, "outside the immutable model"
                    ),
                ):
                    write_speech_robustness_asr_report(report, output)
            self.assertEqual(sha256_control_path(report.model_directory), expected_hash)
            self.assertEqual(
                list(report.model_directory.iterdir()),
                [report.model_directory / "weights.bin"],
            )
            self.assertNotIn("model_directory", report.to_dict())
            output = root / "report.json"
            self.assertEqual(write_speech_robustness_asr_report(report, output), output)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), report.to_dict()
            )

    def test_directory_aliases_preserve_immutable_model_and_corpus_guards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            corpus_alias = root / "corpus-alias"
            model_alias = root / "model-alias"
            symlink_or_skip(corpus_alias, root / "corpus", target_is_directory=True)
            symlink_or_skip(model_alias, root / "model", target_is_directory=True)
            report = _report(root, model=model_alias)
            self.assertEqual(report.corpus_directory, root / "corpus")
            self.assertEqual(report.model_directory, root / "model")
            for directory, label in ((corpus_alias, "corpus"), (model_alias, "model")):
                with (
                    self.subTest(directory=directory),
                    self.assertRaisesRegex(
                        SpeechRobustnessAsrError, f"outside the immutable {label}"
                    ),
                ):
                    write_speech_robustness_asr_report(
                        report, directory / "report.json"
                    )


if __name__ == "__main__":
    unittest.main()
