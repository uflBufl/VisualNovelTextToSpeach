import hashlib
import json
import threading
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.preview_content_advisor import (
    WHISPER_TINY_EN,
    PreviewContentAdvisor,
    PreviewContentCheckCancelled,
    PreviewContentCheckError,
)
from vntts.speech_content_advice import SpeechContentAdvice
from vntts.support import PerformanceLog, performance_operation_id


@dataclass(frozen=True)
class Preview:
    identity: str
    candidate_source_id: str
    text: str
    audio_sha256: str
    path: Path
    seed: int | None = 0


class PreviewContentAdvisorTest(unittest.TestCase):
    def test_advice_reports_cold_and_warm_cost_in_enclosing_operation(self):
        performance = PerformanceLog()
        token = performance_operation_id.set("plan-operation")
        try:
            with patch("vntts.support.performance_log", performance):
                first = self.advisor.check((self.preview,))
                self.assertEqual(self.advisor.check((self.preview,)), first)
        finally:
            performance_operation_id.reset(token)
        events = performance.report()["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event["operation_id"] for event in events], ["plan-operation"] * 2
        )
        self.assertEqual(
            [(event["cache_hits"], event["cache_misses"]) for event in events],
            [(0, 1), (1, 0)],
        )
        self.assertNotIn(self.preview.text, repr(events))
        self.assertNotIn(self.preview.audio_sha256, repr(events))

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.preview = self._preview("a", "Wait what", b"first audio")
        self.transcribe = Mock(return_value="Wait what Wine")
        self.factory = Mock(return_value=self.transcribe)
        self.resolve = Mock(return_value=self.root / "model")
        self.install = Mock()
        self.status = Mock(return_value={"status": "missing"})
        self.advisor = PreviewContentAdvisor(
            self.root,
            transcriber_factory=self.factory,
            model_resolver=self.resolve,
            model_installer=self.install,
            model_status=self.status,
        )

    def _preview(self, character, text, payload):
        identity = character * 64
        path = self.root / f"{identity}.wav"
        path.write_bytes(payload)
        preview = Preview(
            identity,
            f"voice-{character}",
            text,
            hashlib.sha256(payload).hexdigest(),
            path,
        )
        self._manifest(preview)
        return preview

    def _manifest(self, preview):
        preview.path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "identity": preview.identity,
                    "audio_sha256": preview.audio_sha256,
                    "seed": preview.seed,
                }
            )
        )

    def _cache(self):
        return self.advisor._cache_path(self.preview.identity)

    def test_batch_reuses_one_model_and_restart_reuses_raw_transcript(self):
        other = self._preview("b", "Wait what", b"second audio")
        checks = self.advisor.check((self.preview, other))
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0].advice.status, "review_possible_content_mismatch")
        self.assertEqual(checks[0].source_id, self.preview.candidate_source_id)
        self.assertEqual(checks[0].seed, self.preview.seed)
        self.factory.assert_called_once_with(self.root / "model")
        self.resolve.assert_called_once_with()
        self.install.assert_not_called()
        self.status.assert_not_called()
        self.assertEqual(self.transcribe.call_count, 2)
        saved = PreviewContentAdvisor(self.root)
        with patch(
            "vntts.preview_content_advisor.resolve_managed_asr_model",
            side_effect=AssertionError("cache must not load a model"),
        ):
            self.assertEqual(saved.lookup(self.preview), checks[0])
            self.assertEqual(
                saved.lookup_saved(
                    self.preview.identity,
                    self.preview.text,
                    self.preview.candidate_source_id,
                ),
                checks[0],
            )
            self.assertEqual(saved.check((self.preview,)), (checks[0],))
        document = json.loads(self._cache().read_text())
        self.assertEqual(document["observed_text"], "Wait what Wine")
        self.assertNotIn("status", document)

    def test_default_checker_sends_a_snapshot_to_an_isolated_batch(self):
        advisor = PreviewContentAdvisor(self.root)
        with patch(
            "vntts.preview_content_worker.check_in_process", return_value=()
        ) as check:
            self.assertEqual(advisor.check((self.preview,), install_model=True), ())
        root, previews, install, cancellation = check.call_args.args
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(previews[0].identity, self.preview.identity)
        self.assertEqual(previews[0].path, self.preview.path)
        self.assertEqual(previews[0].audio_sha256, self.preview.audio_sha256)
        self.assertIsNot(previews[0], self.preview)
        self.assertTrue(install)
        self.assertIsNone(cancellation)
        self.factory.assert_not_called()

    def test_cached_and_empty_default_batches_never_start_a_process(self):
        expected = self.advisor.check((self.preview,))
        with patch(
            "vntts.preview_content_worker.check_in_process",
            side_effect=AssertionError("cached advice must not start a worker"),
        ):
            advisor = PreviewContentAdvisor(self.root)
            self.assertEqual(advisor.check((self.preview,)), expected)
            self.assertEqual(advisor.check(()), ())

    def test_each_injected_hook_keeps_the_batch_in_process(self):
        for name in (
            "transcriber_factory",
            "model_resolver",
            "model_installer",
            "model_status",
        ):
            with self.subTest(hook=name):
                advisor = PreviewContentAdvisor(self.root, **{name: Mock()})
                with (
                    patch.object(advisor, "_check_batch", return_value=()) as local,
                    patch(
                        "vntts.preview_content_worker.check_in_process",
                        side_effect=AssertionError("injected hooks must remain local"),
                    ),
                ):
                    self.assertEqual(advisor.check((self.preview,)), ())
                local.assert_called_once_with((self.preview,), False, None)

    def test_lookup_recomputes_advice_from_raw_transcript(self):
        self.advisor.check((self.preview,))
        current = SpeechContentAdvice("expected", "raw", "uncertain_recognition", ())
        with patch(
            "vntts.preview_content_advisor.assess_speech_content", return_value=current
        ) as assess:
            self.assertEqual(self.advisor.lookup(self.preview).advice, current)
        assess.assert_called_once_with("Wait what", "Wait what Wine")

    def test_model_and_policy_versions_invalidate_cache(self):
        self.advisor.check((self.preview,))
        for changed in (
            {"SPEECH_CONTENT_ADVICE_VERSION": 999},
            {"WHISPER_TINY_EN": replace(WHISPER_TINY_EN, tree_sha256="f" * 64)},
            {"WHISPER_TINY_EN": replace(WHISPER_TINY_EN, revision="f" * 40)},
        ):
            with (
                self.subTest(changed=changed),
                patch.multiple("vntts.preview_content_advisor", **changed),
            ):
                self.assertIsNone(self.advisor.lookup(self.preview))

    def test_cache_is_bound_to_exact_text_source_identity_and_actual_audio(self):
        self.advisor.check((self.preview,))
        for changed in (
            replace(self.preview, text="not ready"),
            replace(self.preview, candidate_source_id="another voice"),
            replace(self.preview, identity="b" * 64),
            replace(self.preview, audio_sha256="f" * 64),
            replace(self.preview, path=self.root / "missing.wav"),
            replace(self.preview, seed=2),
            replace(self.preview, seed=False),
            replace(self.preview, identity="x" * 64),
            replace(self.preview, text=""),
            replace(self.preview, candidate_source_id=" "),
        ):
            with self.subTest(changed=changed):
                self.assertIsNone(self.advisor.lookup(changed))
        self.preview.path.write_bytes(b"changed audio")
        self.assertIsNone(self.advisor.lookup(self.preview))
        with self.assertRaisesRegex(PreviewContentCheckError, "changed"):
            self.advisor.check((self.preview,))
        self.assertEqual(self.transcribe.call_count, 1)

    def test_audio_changed_during_inference_never_writes_advice(self):
        def mutate(_payload):
            self.preview.path.write_bytes(b"replacement audio")
            return self.preview.text

        self.transcribe.side_effect = mutate
        with self.assertRaisesRegex(PreviewContentCheckError, "changed"):
            self.advisor.check((self.preview,))
        self.assertFalse(self._cache().exists())

    def test_malformed_or_stale_cache_is_a_miss(self):
        self.advisor.check((self.preview,))
        original = json.loads(self._cache().read_text())
        for malformed in (
            "{",
            "[]",
            "null",
            json.dumps({}),
            json.dumps({**original, "observed_text": None}),
            json.dumps({**original, "analyzer": {}}),
            json.dumps({**original, "analyzer": {"revision": float("nan")}}),
            json.dumps({**original, "seed": False}),
            json.dumps(
                {key: value for key, value in original.items() if key != "seed"}
            ),
        ):
            with self.subTest(malformed=malformed):
                self._cache().write_text(malformed)
                self.assertIsNone(self.advisor.lookup(self.preview))
        self.advisor.check((self.preview,))
        self.assertIsNotNone(self.advisor.lookup(self.preview))
        self.assertEqual(self.transcribe.call_count, 2)

    def test_generation_manifest_is_required_before_asr_or_cache_admission(self):
        self.advisor.check((self.preview,))
        manifest = self.preview.path.with_suffix(".json")
        original = json.loads(manifest.read_text())
        for invalid in (
            None,
            "{",
            "[]",
            json.dumps({**original, "identity": "b" * 64}),
            json.dumps({**original, "audio_sha256": "f" * 64}),
            json.dumps({**original, "seed": 2}),
            json.dumps({**original, "seed": False}),
            json.dumps({**original, "seed": -1}),
            json.dumps({**original, "seed": 2**64}),
            json.dumps(
                {key: value for key, value in original.items() if key != "seed"}
            ),
            " " * 1025,
        ):
            with self.subTest(invalid=invalid):
                manifest.unlink(missing_ok=True)
                if invalid is not None:
                    manifest.write_text(invalid)
                self.assertIsNone(self.advisor.lookup(self.preview))
                self.assertIsNone(
                    self.advisor.lookup_saved(
                        self.preview.identity,
                        self.preview.text,
                        self.preview.candidate_source_id,
                    )
                )
                with self.assertRaises(PreviewContentCheckError):
                    self.advisor.check((self.preview,))
        self.transcribe.assert_called_once()

    def test_changed_generation_seed_invalidates_saved_advice(self):
        self.advisor.check((self.preview,))
        changed = replace(self.preview, seed=3)
        self._manifest(changed)
        self.assertIsNone(self.advisor.lookup(changed))
        self.assertIsNone(
            self.advisor.lookup_saved(
                changed.identity, changed.text, changed.candidate_source_id
            )
        )
        checked = self.advisor.check((changed,))[0]
        self.assertEqual(checked.seed, 3)
        self.assertEqual(
            self.advisor.lookup_saved(
                changed.identity, changed.text, changed.candidate_source_id
            ),
            checked,
        )
        self.assertEqual(self.transcribe.call_count, 2)

    def test_null_seed_is_preserved_for_backend_without_seed(self):
        preview = replace(self.preview, seed=None)
        self._manifest(preview)
        checked = self.advisor.check((preview,))[0]
        self.assertIsNone(checked.seed)
        self.assertEqual(
            self.advisor.lookup_saved(
                preview.identity, preview.text, preview.candidate_source_id
            ),
            checked,
        )

    def test_manifest_changed_during_inference_never_writes_advice(self):
        def mutate(_payload):
            self._manifest(replace(self.preview, seed=4))
            return self.preview.text

        self.transcribe.side_effect = mutate
        with self.assertRaises(PreviewContentCheckError):
            self.advisor.check((self.preview,))
        self.assertFalse(self._cache().exists())

    def test_saved_lookup_rejects_path_traversal_and_wrong_binding(self):
        self.advisor.check((self.preview,))
        for identity, text, source in (
            ("../elsewhere", self.preview.text, self.preview.candidate_source_id),
            (self.preview.identity, "other text", self.preview.candidate_source_id),
            (self.preview.identity, self.preview.text, "other voice"),
        ):
            self.assertIsNone(self.advisor.lookup_saved(identity, text, source))
        self.preview.path.unlink()
        self.assertIsNone(
            self.advisor.lookup_saved(
                self.preview.identity,
                self.preview.text,
                self.preview.candidate_source_id,
            )
        )

    def test_cancellation_before_or_after_inference_never_writes_clean_advice(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(PreviewContentCheckCancelled):
            self.advisor.check((self.preview,), cancel_event=cancelled)
        self.factory.assert_not_called()
        cancelled.clear()

        def cancel(_payload):
            cancelled.set()
            return self.preview.text

        self.transcribe.side_effect = cancel
        with self.assertRaises(PreviewContentCheckCancelled):
            self.advisor.check((self.preview,), cancel_event=cancelled)
        self.assertFalse(self._cache().exists())

    def test_failed_or_invalid_transcripts_are_not_clean_and_not_cached(self):
        for result in (RuntimeError("checker failed"), None):
            with self.subTest(result=result):
                self.transcribe.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.transcribe.return_value = result
                with self.assertRaises(PreviewContentCheckError):
                    self.advisor.check((self.preview,))
                self.assertFalse(self._cache().exists())
        self.transcribe.side_effect = None
        self.transcribe.return_value = ""
        check = self.advisor.check((self.preview,))[0]
        self.assertEqual(check.advice.status, "uncertain_recognition")

    def test_install_is_explicit_and_only_for_missing_checker(self):
        self.advisor.check((self.preview,), install_model=True)
        self.install.assert_called_once_with()
        self.status.assert_called_once_with()
        self._cache().unlink()
        self.status.return_value = {"status": "invalid"}
        self.resolve.side_effect = RuntimeError("corrupt model")
        with self.assertRaisesRegex(PreviewContentCheckError, "corrupt model"):
            self.advisor.check((self.preview,), install_model=True)
        self.assertEqual(self.install.call_count, 1)
        self.assertFalse(self._cache().exists())

    def test_replaced_context_can_cancel_while_waiting_for_running_checker(self):
        waiting, cancelled = threading.Event(), threading.Event()
        errors = []

        class Cancellation:
            def is_set(self):
                waiting.set()
                return cancelled.is_set()

        def check():
            try:
                self.advisor.check((self.preview,), cancel_event=Cancellation())
            except Exception as error:
                errors.append(error)

        with exclusive_advisory_lock(self.root / "content-advice" / "checker.lock"):
            worker = threading.Thread(target=check)
            worker.start()
            try:
                self.assertTrue(waiting.wait(5))
                self.factory.assert_not_called()
            finally:
                cancelled.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PreviewContentCheckCancelled)
        self.assertFalse(self._cache().exists())

    def test_concurrent_checks_only_transcribe_once(self):
        entered, release = threading.Event(), threading.Event()
        outputs, errors = [], []

        def transcribe(_payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return self.preview.text

        def check():
            try:
                outputs.append(self.advisor.check((self.preview,)))
            except Exception as error:
                errors.append(error)

        self.transcribe.side_effect = transcribe
        first = threading.Thread(target=check)
        second = threading.Thread(target=check)
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0], outputs[1])
        self.transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
