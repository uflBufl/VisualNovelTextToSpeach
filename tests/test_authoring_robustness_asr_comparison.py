import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import vntts.authoring.robustness_asr as asr
from tests.test_authoring_robustness_corpus import _decision, _pending_workspace
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.robustness_corpus import publish_speech_robustness_corpus


def _identity(document, field):
    document[field] = canonical_document_sha256(
        {key: value for key, value in document.items() if key != field}
    )


class SpeechRobustnessAsrComparisonTest(unittest.TestCase):
    def test_standalone_digits_match_english_words_in_both_directions(self):
        for digit, word in zip(
            "0123456789",
            "zero one two three four five six seven eight nine".split(),
            strict=True,
        ):
            for expected, observed in (
                (f"Wait, {digit}.", f"WAIT {word}!"),
                (f"Wait, {word}.", f"WAIT {digit}!"),
            ):
                with self.subTest(expected=expected, observed=observed):
                    comparison = asr.compare_speech_transcript(expected, observed)
                    self.assertEqual(comparison["distance"], 0)
                    self.assertEqual(comparison["expected_word_count"], 2)
                    self.assertEqual(comparison["observed_word_count"], 2)
        self.assertEqual(
            asr.compare_speech_transcript("6 and 7", "six and seven")["distance"],
            0,
        )
        for text in ("Wait. 7", "Wait (7)", "Wait [7]"):
            with self.subTest(text=text):
                self.assertEqual(
                    asr.compare_speech_transcript(text, "Wait seven")["distance"], 0
                )

    def test_number_formatting_does_not_hide_meaningful_word_changes(self):
        for expected, observed, distance in (
            ("No, seven.", "Now, 7.", 1),
            ("close six", "closed 6", 1),
            ("six", "seven", 1),
            ("6", "7", 1),
            ("Count seven", "Count 7 again", 1),
            ("7", "", 1),
        ):
            with self.subTest(expected=expected, observed=observed):
                self.assertEqual(
                    asr.compare_speech_transcript(expected, observed)["distance"],
                    distance,
                )
        comparison = asr.compare_speech_transcript("Count seven", "Count 7 again")
        self.assertEqual(comparison["insertions"], 1)

    def test_compound_signed_and_embedded_numbers_keep_legacy_tokens(self):
        for text, words in (
            ("6.7", "six seven"),
            ("6,7", "six seven"),
            (".7", "seven"),
            ("7/6/2026", "seven six 2026"),
            ("7-6-2026", "seven six 2026"),
            ("7:06", "seven 06"),
            ("-7", "seven"),
            ("- 7", "seven"),
            ("−7", "seven"),
            ("+7", "seven"),
            ("7 800 123 45 67", "seven 800 123 45 67"),
            ("+7 (800) 123-45-67", "seven 800 123 45 67"),
            ("(7) 800-123", "seven 800 123"),
            ("17", "seventeen"),
            ("７", "seven"),
            ("R7", "R seven"),
            ("7th", "seventh"),
            ("_7", "seven"),
            ("7's", "seven's"),
            ("v1.7", "v1 seven"),
            ("7.1b", "seven 1b"),
        ):
            for expected, observed in ((text, words), (words, text)):
                with self.subTest(expected=expected, observed=observed):
                    self.assertEqual(
                        asr.compare_speech_transcript(expected, observed),
                        asr.compare_speech_transcript(
                            expected, observed, analysis_version=2
                        ),
                    )
                    self.assertGreater(
                        asr.compare_speech_transcript(expected, observed)["distance"],
                        0,
                    )

    def test_comparison_version_is_explicit_and_exact(self):
        self.assertEqual(asr._words("Wait 7"), ("wait", "7"))
        self.assertEqual(
            asr.compare_speech_transcript("seven", "7", analysis_version=2)[
                "substitutions"
            ],
            1,
        )
        for version in (1, 4, True, 2.0, 3.0, "3", None):
            with self.subTest(version=version):
                with self.assertRaisesRegex(asr.SpeechRobustnessAsrError, "version"):
                    asr.compare_speech_transcript(
                        "seven", "7", analysis_version=version
                    )

    def test_report_version_binds_comparison_and_preserves_historical_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, queue_id = _pending_workspace(
                root / "reviewed", text="Wait seven"
            )
            _decision(workspace, queue_id)
            corpus = root / "corpus"
            publish_speech_robustness_corpus([workspace / "cohort-reviews"], [], corpus)
            model = root / "model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"model")
            progress = root / "progress.json"
            report = asr.build_speech_robustness_asr_report(
                corpus, model, transcriber=lambda _: "Wait 7", progress_path=progress
            )
            self.assertEqual(report.document["schema_version"], 3)
            self.assertEqual(report.document["records"][0]["comparison"]["distance"], 0)
            self.assertEqual(
                report.document["records"][0]["expected_text"], "Wait seven"
            )
            self.assertEqual(report.document["records"][0]["observed_text"], "Wait 7")

            historical = report.to_dict()
            historical["schema_version"] = 2
            historical["records"][0]["comparison"] = asr.compare_speech_transcript(
                "Wait seven", "Wait 7", analysis_version=2
            )
            historical["summary"] = asr._summary(historical["records"])
            _identity(historical, "report_id")
            self.assertNotEqual(historical["report_id"], report.report_id)
            frozen = copy.deepcopy(historical)
            output = root / "historical-report.json"
            asr.write_speech_robustness_asr_report(
                replace(report, report_id=historical["report_id"], document=historical),
                output,
            )
            self.assertEqual(json.loads(output.read_text()), frozen)
            self.assertEqual(historical, frozen)

            for document, version in ((historical, 3), (report.to_dict(), 2)):
                forged = copy.deepcopy(document)
                forged["schema_version"] = version
                _identity(forged, "report_id")
                with self.subTest(version=version):
                    with self.assertRaisesRegex(
                        asr.SpeechRobustnessAsrError, "comparison is invalid"
                    ):
                        asr._validate_document(forged)

            old_progress = json.loads(progress.read_text())
            old_progress["schema_version"] = 2
            old_progress["records"] = historical["records"]
            _identity(old_progress, "progress_id")
            progress.write_text(json.dumps(old_progress))
            frozen_progress = progress.read_bytes()
            with self.assertRaisesRegex(
                asr.SpeechRobustnessAsrError, "numeric transcript normalization"
            ):
                asr.build_speech_robustness_asr_report(
                    corpus,
                    model,
                    transcriber=lambda _: self.fail("legacy progress resumed"),
                    progress_path=progress,
                )
            self.assertEqual(progress.read_bytes(), frozen_progress)
            resumed = asr.build_speech_robustness_asr_report(
                corpus,
                model,
                transcriber=lambda _: "Wait 7",
                progress_path=root / "new-progress.json",
            )
            self.assertEqual(resumed.document, report.document)
            self.assertEqual(progress.read_bytes(), frozen_progress)


if __name__ == "__main__":
    unittest.main()
