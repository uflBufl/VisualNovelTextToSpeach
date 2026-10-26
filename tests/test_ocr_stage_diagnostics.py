import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from vntts import ocr_worker
from vntts.controller import AppController
from vntts.ocr import OCRPreviewCancelled, OCRResult


class OCRWorkerStageDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.owner = ocr_worker.PersistentOCR()
        self.image = Image.new("RGB", (10, 10))
        self.image.info["capture_provenance"] = {
            "run_id": "run-original",
            "capture_id": "capture-original",
            "window_title": "private window",
        }
        self.owner._lock = Mock()
        self.owner._lock.acquire.return_value = True
        self.result = OCRResult("Speaker", "private dialogue", 95, "balanced", 1)

    def test_one_success_event_separates_queue_from_pipeline_time(self):
        self.owner._lock.acquire.side_effect = [False, True]

        def pipeline(image, *_args, **_kwargs):
            image.info["capture_provenance"] = {"run_id": "later-run"}
            return self.result

        with (
            patch.object(ocr_worker, "_ocr_budget", return_value=Mock(return_value=1)),
            patch.object(ocr_worker, "monotonic", side_effect=[10, 10.005, 10.025]),
            patch.object(ocr_worker, "recognize_pipeline", side_effect=pipeline),
            patch.object(ocr_worker, "record_diagnostic_event") as record,
        ):
            self.assertIs(self.owner.recognize(self.image), self.result)
        record.assert_called_once_with(
            "ocr",
            "worker_request_completed",
            run_id="run-original",
            capture_id="capture-original",
            worker_pid=None,
            outcome="success",
            error_type=None,
            queue_wait_ms=5.0,
            worker_call_ms=20.0,
        )
        self.owner._lock.release.assert_called_once()

    def test_queued_cancellation_does_not_discard_active_worker(self):
        cancelled = OCRPreviewCancelled("cancelled")
        self.owner._lock.acquire.return_value = False
        with (
            patch.object(
                ocr_worker,
                "_ocr_budget",
                return_value=Mock(side_effect=[1, cancelled]),
            ),
            patch.object(ocr_worker, "monotonic", side_effect=[10, 10.010]),
            patch.object(self.owner, "_discard") as discard,
            patch.object(ocr_worker, "record_diagnostic_event") as record,
        ):
            with self.assertRaises(OCRPreviewCancelled) as raised:
                self.owner.recognize(self.image)
        self.assertIs(raised.exception, cancelled)
        discard.assert_not_called()
        self.owner._lock.release.assert_not_called()
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["outcome"], "cancelled")
        self.assertEqual(record.call_args.kwargs["queue_wait_ms"], 10)
        self.assertEqual(record.call_args.kwargs["worker_call_ms"], 0)

    def test_active_failures_report_once_and_preserve_exception_and_cleanup(self):
        for failure, outcome in (
            (OCRPreviewCancelled("cancelled"), "cancelled"),
            (TimeoutError("timeout"), "timeout"),
            (RuntimeError("private details"), "error"),
            (KeyboardInterrupt(), "error"),
        ):
            with (
                self.subTest(outcome=outcome, failure=type(failure).__name__),
                patch.object(
                    ocr_worker, "_ocr_budget", return_value=Mock(return_value=1)
                ),
                patch.object(ocr_worker, "monotonic", side_effect=[10, 10.005, 10.025]),
                patch.object(ocr_worker, "recognize_pipeline", side_effect=failure),
                patch.object(self.owner, "_discard") as discard,
                patch.object(ocr_worker, "record_diagnostic_event") as record,
            ):
                with self.assertRaises(type(failure)) as raised:
                    self.owner.recognize(self.image)
                self.assertIs(raised.exception, failure)
                discard.assert_called_once()
                record.assert_called_once()
                self.assertEqual(record.call_args.kwargs["outcome"], outcome)
                self.assertEqual(
                    record.call_args.kwargs["error_type"], type(failure).__name__
                )
                self.assertEqual(record.call_args.kwargs["worker_call_ms"], 20)
                self.assertNotIn("private", repr(record.call_args))
        self.assertEqual(self.owner._lock.release.call_count, 4)

    def test_broken_diagnostics_preserve_success_and_active_failure_cleanup(self):
        for failure in (None, RuntimeError("recognition failed")):
            with (
                self.subTest(failure=failure),
                patch.object(
                    ocr_worker, "_ocr_budget", return_value=Mock(return_value=1)
                ),
                patch.object(
                    ocr_worker,
                    "recognize_pipeline",
                    return_value=self.result,
                    side_effect=failure,
                ),
                patch.object(self.owner, "_discard") as discard,
                patch.object(
                    ocr_worker,
                    "record_diagnostic_event",
                    side_effect=OSError("recorder failed"),
                ),
            ):
                if failure is None:
                    self.assertIs(self.owner.recognize(self.image), self.result)
                    discard.assert_not_called()
                else:
                    with self.assertRaises(RuntimeError) as raised:
                        self.owner.recognize(self.image)
                    self.assertIs(raised.exception, failure)
                    discard.assert_called_once()
        self.assertEqual(self.owner._lock.release.call_count, 2)


class StoryResultResolutionDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (10, 10))
        self.image.info["capture_provenance"] = {
            "run_id": "run-original",
            "capture_id": "capture-original",
            "window_title": "private window",
        }
        self.frame = SimpleNamespace(image=self.image)
        self.result = OCRResult("Speaker", "private dialogue", 95, "balanced", 1)
        self.controller = SimpleNamespace(
            voice_router=SimpleNamespace(registry=object()),
            settings=SimpleNamespace(ocr_minimum_confidence=60, ocr_language="eng"),
            _reject_uncertain_live_frame=Mock(),
            uncertain_frame_recorder=None,
            _publish_diagnostic=Mock(),
            _resolve_voice_label=Mock(),
            correction_dictionary=None,
            chapter_voice_preloader=None,
            _canonical_observed_character=lambda character, text: character,
        )

    def recognize(self, _frame, *_args, result_resolver, **_kwargs):
        resolved = result_resolver(self.result)
        return resolved.character, resolved.text

    def test_resolution_records_original_capture_and_returns_resolved_result(self):
        resolved = OCRResult("Canonical", "canonical dialogue", 95, "balanced", 1)

        def resolve(_result):
            self.image.info["capture_provenance"] = {"run_id": "later-run"}
            return resolved

        self.controller._resolve_story_ocr_result = Mock(side_effect=resolve)
        with (
            patch("vntts.controller.recognize_live_frame", side_effect=self.recognize),
            patch("vntts.controller.monotonic", side_effect=[20, 20.017]),
            patch("vntts.controller.record_diagnostic_event") as record,
        ):
            actual = AppController._recognize_live_frame(self.controller, self.frame)
        self.assertEqual(actual, (resolved.character, resolved.text))
        record.assert_called_once_with(
            "ocr",
            "story-result-resolution",
            run_id="run-original",
            capture_id="capture-original",
            outcome="success",
            error_type=None,
            resolver_ms=17.0,
        )

    def test_broken_capture_metadata_does_not_change_result_or_resolver_failure(self):
        class Frame:
            image = self.image

            @property
            def provenance(self):
                raise RuntimeError("capture metadata failed")

        self.image.info.clear()
        for failure in (None, RuntimeError("resolver failed")):
            with self.subTest(failure=failure):
                self.controller._resolve_story_ocr_result = Mock(
                    return_value=self.result, side_effect=failure
                )
                with (
                    patch(
                        "vntts.controller.recognize_live_frame",
                        side_effect=self.recognize,
                    ),
                    patch("vntts.controller.record_diagnostic_event") as record,
                ):
                    if failure is None:
                        actual = AppController._recognize_live_frame(
                            self.controller, Frame()
                        )
                        self.assertEqual(
                            actual, (self.result.character, self.result.text)
                        )
                    else:
                        with self.assertRaises(RuntimeError) as raised:
                            AppController._recognize_live_frame(
                                self.controller, Frame()
                            )
                        self.assertIs(raised.exception, failure)
                record.assert_called_once()
                self.assertNotIn("run_id", record.call_args.kwargs)
                self.assertNotIn("capture_id", record.call_args.kwargs)

    def test_resolver_failure_and_broken_recorder_preserve_signal(self):
        for failure in (None, RuntimeError("resolver failed"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                self.controller._resolve_story_ocr_result = Mock(
                    return_value=self.result, side_effect=failure
                )
                with (
                    patch(
                        "vntts.controller.recognize_live_frame",
                        side_effect=self.recognize,
                    ),
                    patch(
                        "vntts.controller.record_diagnostic_event",
                        side_effect=OSError("recorder failed"),
                    ) as record,
                ):
                    if failure is None:
                        self.assertEqual(
                            AppController._recognize_live_frame(
                                self.controller, self.frame
                            ),
                            (self.result.character, self.result.text),
                        )
                    else:
                        with self.assertRaises(type(failure)) as raised:
                            AppController._recognize_live_frame(
                                self.controller, self.frame
                            )
                        self.assertIs(raised.exception, failure)
                record.assert_called_once()
                self.assertEqual(
                    record.call_args.kwargs["outcome"],
                    "success" if failure is None else "error",
                )


if __name__ == "__main__":
    unittest.main()
