import hashlib
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAuditError,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    publish_failure_reference_audit,
    record_failure_reference_decision,
)
from vntts.authoring.failure_reference_binding import (
    load_failure_reference_binding_document,
    publish_failure_reference_binding,
)
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreviewCancelled,
    FailureReferencePreviewService,
)
from vntts.authoring.listening import record_trial_preference
from vntts.authoring.reference_render_comparison import (
    REFERENCE_RENDER_INPUT_SCHEMA,
    REFERENCE_RENDER_INPUT_VERSION,
    ReferenceRenderComparisonError,
    create_reference_render_listening,
    import_reference_render_preference,
    load_reference_render_plan,
    publish_reference_render_comparison,
)
from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)


class _CollectedResult:
    def __init__(self, result_factory):
        self.result_factory = result_factory

    def collect(self):
        return self.result_factory()


class _PreviewBackend:
    def __init__(self, name, registry, model_name, cancellation, *, on_render=None):
        self.name = name
        self.registry = registry
        self.model_name = model_name
        self.cancellation = cancellation
        self.on_render = on_render
        self.requests = []
        self.stop_calls = 0

    def render(self, request):
        self.requests.append(request)

        def result():
            if self.on_render is not None:
                self.on_render(self, request)
            completion = (
                SynthesisCompletion.CANCELLED
                if request.cancellation_requested()
                else SynthesisCompletion.COMPLETE
            )
            return SynthesisResult(
                pcm=np.full((800, 1), 0.2, dtype=np.float32),
                sample_rate=16_000,
                completion=completion,
                limits=SynthesisLimits(100, 2.0),
                timing=SynthesisTiming(10.0, 20.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=800,
                ),
            )

        return _CollectedResult(result)

    def stop(self):
        self.stop_calls += 1


class _PreviewBackendFactory:
    def __init__(self, *, on_render=None):
        self.on_render = on_render
        self.backends = []

    def __call__(
        self,
        name,
        registry,
        _cache_root,
        *,
        model_name=None,
        startup_cancellation=None,
        **_options,
    ):
        backend = _PreviewBackend(
            name,
            registry,
            model_name,
            startup_cancellation,
            on_render=self.on_render,
        )
        self.backends.append(backend)
        return backend


class FailureReferenceAuditTest(unittest.TestCase):
    def create_failed_workspace(self, root):
        _fixture, _imported, created = create_test_workspace(root)
        state_path = created.directory / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text())
        queue_id, result = next(iter(state["items"].items()))
        for field in ("path", "file_sha256", "quality", "review_status"):
            result.pop(field, None)
        result.update(
            {
                "status": "failed",
                "provider": "moss-tts",
                "model": "model",
                "generation_profile": "stable",
                "voice_character": "Rhiannon",
                "synthesis_provenance_sha256": "a" * 64,
                "failure": {
                    "schema_version": 1,
                    "kind": "speech_silence",
                    "completion": "complete",
                    "error_type": "SpeechSilenceValidationError",
                    "speech_quality": {
                        "leading_silence_seconds": 0.0,
                        "trailing_silence_seconds": 0.0,
                        "longest_internal_silence_seconds": 2.0,
                        "silence_ratio": 0.4,
                    },
                    "text_features": {
                        "word_count": 4,
                        "character_count": 20,
                        "sentence_boundary_count": 1,
                        "comma_count": 0,
                        "ellipsis_count": 0,
                    },
                },
            }
        )
        state_path.write_text(json.dumps(state, sort_keys=True))
        return created.directory, queue_id

    def test_audit_binds_case_candidates_and_private_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            output = root / "audit"

            result = publish_failure_reference_audit(workspace, output, seed=7)
            loaded = load_failure_reference_audit(output)
            document = json.loads((output / "audit.json").read_text())

        self.assertEqual(result, loaded)
        self.assertEqual(document["case_count"], 1)
        self.assertEqual(document["groups"][0]["cases"][0]["queue_id"], queue_id)
        self.assertIn("neither_acceptable", document["groups"][0]["decision_options"])

    def test_explicit_audit_scope_accepts_only_current_failed_queue_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)

            scoped = publish_failure_reference_audit(
                workspace, root / "scoped", queue_ids=(queue_id,)
            )
            with self.assertRaisesRegex(
                FailureReferenceAuditError, "not current failures"
            ):
                publish_failure_reference_audit(
                    workspace, root / "missing", queue_ids=("missing",)
                )

        self.assertEqual(scoped.case_count, 1)

    def test_explicit_audit_normalizes_legacy_string_only_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            item = state["items"][queue_id]
            item.pop("failure")
            item["last_error"] = "Legacy output failed speech quality: silence"
            state_path.write_text(json.dumps(state, sort_keys=True))

            publish_failure_reference_audit(
                workspace,
                root / "audit",
                queue_ids=(queue_id,),
            )
            case = json.loads((root / "audit/audit.json").read_text())["groups"][0][
                "cases"
            ][0]

            self.assertEqual(case["failure"]["kind"], "speech_silence")
            self.assertEqual(case["failure"]["error_type"], "LegacyStringFailure")
            self.assertTrue(case["failure"]["inferred_from_legacy_error"])

    def test_audio_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            document = json.loads((output / "audit.json").read_text())
            audio = output / document["groups"][0]["candidates"][0]["audio"]
            audio.write_bytes(b"changed")

            with self.assertRaisesRegex(FailureReferenceAuditError, "audio changed"):
                load_failure_reference_audit(output)

    def test_private_mapping_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            key_path = output / ".blind-key.json"
            key = json.loads(key_path.read_text())
            key["groups"][0]["candidates"][0]["source_reference"] = "forged.wav"
            key_path.write_text(json.dumps(key))

            with self.assertRaisesRegex(FailureReferenceAuditError, "blind key"):
                load_failure_reference_audit(output)

    def test_candidate_and_neither_decisions_are_exact_and_checksum_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            audit = json.loads((output / "audit.json").read_text())
            group = audit["groups"][0]
            candidate = group["candidates"][0]

            first = record_failure_reference_decision(
                output, group["group_id"], candidate["candidate_id"]
            )
            self.assertEqual(first, load_failure_reference_decisions(output))
            self.assertEqual(
                first["decisions"][0]["selected_reference_sha256"],
                candidate["sha256"],
            )
            self.assertEqual(first["decisions"][0]["case_queue_ids"], [queue_id])
            self.assertEqual(first["schema_version"], 3)

            legacy = {**first, "schema_version": 2}
            legacy.pop("decision_set_id")
            legacy["decision_set_id"] = hashlib.sha256(
                json.dumps(
                    legacy,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            (output / "decisions.json").write_text(json.dumps(legacy))
            self.assertEqual(
                load_failure_reference_decisions(output)["schema_version"], 2
            )

            second = record_failure_reference_decision(
                output, group["group_id"], "neither_acceptable"
            )
            self.assertIsNone(second["decisions"][0]["selected_reference_sha256"])

            decisions_path = output / "decisions.json"
            decisions = json.loads(decisions_path.read_text())
            decisions["decisions"][0]["case_queue_ids"] = ["forged"]
            decisions_path.write_text(json.dumps(decisions))
            with self.assertRaisesRegex(
                FailureReferenceAuditError, "decision identity changed"
            ):
                load_failure_reference_decisions(output)

    def test_unrelated_state_review_does_not_invalidate_exact_failure_cases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            state["audit_unrelated_diagnostic"] = {"refresh": 1}
            state_path.write_text(json.dumps(state, sort_keys=True))
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)

            state = json.loads(state_path.read_text())
            state["audit_unrelated_diagnostic"]["refresh"] = 2
            state_path.write_text(json.dumps(state, sort_keys=True))
            self.assertEqual(load_failure_reference_audit(output).case_count, 1)

            state = json.loads(state_path.read_text())
            state["items"][queue_id]["attempts"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True))
            with self.assertRaisesRegex(FailureReferenceAuditError, queue_id):
                load_failure_reference_audit(output)

    def test_preview_is_ephemeral_exact_and_cached_for_the_dialog_lifetime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            document = json.loads((output / "audit.json").read_text())
            group = document["groups"][0]
            candidate = group["candidates"][0]
            text = group["cases"][0]["text"]
            state_path = workspace / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            factory = _PreviewBackendFactory()
            service = FailureReferencePreviewService(output, backend_factory=factory)

            first = service.generate(group["group_id"], candidate["candidate_id"], text)
            second = service.generate(
                group["group_id"], candidate["candidate_id"], text
            )
            reference = (
                factory.backends[0]
                .registry.resolve(f"Reference candidate {candidate['sha256'][:16]}")
                .references[0]
            )
            service.close()

            self.assertEqual(first, second)
            self.assertEqual(len(factory.backends), 1)
            self.assertEqual(len(factory.backends[0].requests), 1)
            self.assertEqual(first.backend, "moss-tts")
            self.assertEqual(first.generation_profile, "stable")
            self.assertEqual(first.seed, 0)
            self.assertTrue(first.payload.startswith(b"RIFF"))
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse((output / "decisions.json").exists())
            self.assertFalse(reference.exists())
            self.assertEqual(factory.backends[0].stop_calls, 1)

    def test_preview_revalidates_candidate_after_render(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            document = json.loads((output / "audit.json").read_text())
            group = document["groups"][0]
            candidate = group["candidates"][0]
            audio = output / candidate["audio"]

            def tamper(_backend, _request):
                audio.write_bytes(b"changed during preview")

            service = FailureReferencePreviewService(
                output, backend_factory=_PreviewBackendFactory(on_render=tamper)
            )
            with self.assertRaisesRegex(FailureReferenceAuditError, "audio changed"):
                service.generate(
                    group["group_id"],
                    candidate["candidate_id"],
                    group["cases"][0]["text"],
                )
            service.close()

    def test_preview_cancellation_has_no_audio_or_authoring_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            document = json.loads((output / "audit.json").read_text())
            group = document["groups"][0]
            candidate = group["candidates"][0]
            state_path = workspace / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            entered = threading.Event()

            def wait_for_cancel(_backend, request):
                entered.set()
                deadline = time.monotonic() + 2.0
                while (
                    not request.cancellation_requested() and time.monotonic() < deadline
                ):
                    time.sleep(0.005)

            service = FailureReferencePreviewService(
                output,
                backend_factory=_PreviewBackendFactory(on_render=wait_for_cancel),
            )
            errors = []

            def generate():
                try:
                    service.generate(
                        group["group_id"],
                        candidate["candidate_id"],
                        group["cases"][0]["text"],
                    )
                except Exception as error:  # noqa: BLE001 - asserted below
                    errors.append(error)

            worker = threading.Thread(target=generate)
            worker.start()
            self.assertTrue(entered.wait(1.0))
            service.cancel()
            worker.join(2.0)
            service.close()

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], FailureReferencePreviewCancelled)
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse((output / "decisions.json").exists())

    def test_publishes_render_only_alternative_reference_comparison(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            audit_root = root / "audit"
            audit = publish_failure_reference_audit(workspace, audit_root, seed=0)
            document = json.loads((audit_root / "audit.json").read_text())
            group = document["groups"][0]
            self.assertEqual(len(group["candidates"]), 2)
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                        "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                        "audit": str(audit_root),
                        "audit_id": audit.audit_id,
                        "arms": [
                            {
                                "arm_id": f"candidate-{index}",
                                "samples": [
                                    {
                                        "queue_id": queue_id,
                                        "case_group_id": group["group_id"],
                                        "candidate_group_id": group["group_id"],
                                        "candidate_id": candidate["candidate_id"],
                                    }
                                ],
                            }
                            for index, candidate in enumerate(
                                group["candidates"], start=1
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path = workspace / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            factory = _PreviewBackendFactory()

            comparison = publish_reference_render_comparison(
                load_reference_render_plan(plan_path),
                root / "comparison",
                backend_factory=factory,
            )
            session = create_reference_render_listening(
                comparison.directory, root / "listening", seed=7
            )

            self.assertEqual(comparison.arm_count, 2)
            self.assertEqual(comparison.sample_count, 1)
            self.assertEqual(comparison.complete_pair_count, 1)
            self.assertTrue(session.is_file())
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(len(factory.backends), 1)
            self.assertEqual(len(factory.backends[0].requests), 2)

            first_report = next(comparison.directory.glob("arms/*/report.json"))
            first_report.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                ReferenceRenderComparisonError, "report changed"
            ):
                create_reference_render_listening(
                    comparison.directory, root / "tampered-listening", seed=7
                )

    def test_imports_exact_blind_preference_into_fresh_audit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            source_audit_root = root / "source-audit"
            source_audit = publish_failure_reference_audit(
                workspace, source_audit_root, seed=0, queue_ids=(queue_id,)
            )
            source_document = json.loads((source_audit_root / "audit.json").read_text())
            source_group = source_document["groups"][0]
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                        "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                        "audit": str(source_audit_root),
                        "audit_id": source_audit.audit_id,
                        "arms": [
                            {
                                "arm_id": f"reference-{index}",
                                "samples": [
                                    {
                                        "queue_id": queue_id,
                                        "case_group_id": source_group["group_id"],
                                        "candidate_group_id": source_group["group_id"],
                                        "candidate_id": candidate["candidate_id"],
                                    }
                                ],
                            }
                            for index, candidate in enumerate(
                                source_group["candidates"], start=1
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            comparison = publish_reference_render_comparison(
                load_reference_render_plan(plan_path),
                root / "comparison",
                backend_factory=_PreviewBackendFactory(),
            )
            session = create_reference_render_listening(
                comparison.directory, root / "listening", seed=7
            )
            trial_id = json.loads(session.read_text())["trials"][0]["trial_id"]
            record_trial_preference(
                session,
                trial_id,
                "a",
                report_path=session.with_name("report.json"),
            )
            selected_assignment = json.loads(
                session.with_name(".blind-key.json").read_text()
            )["assignments"][0]["a"]
            selected_arm = selected_assignment["model_id"]
            selected_reference_sha256 = next(
                value["reference_sha256"]
                for arm in json.loads(
                    (comparison.directory / "comparison.json").read_text()
                )["arms"]
                if arm["arm_id"] == selected_arm
                for value in arm["renders"]
                if value["id"] == queue_id
            )
            fresh_audit_root = root / "fresh-audit"
            fresh_audit = publish_failure_reference_audit(
                workspace, fresh_audit_root, seed=19, queue_ids=(queue_id,)
            )

            imported = import_reference_render_preference(
                fresh_audit_root,
                comparison.directory,
                session,
                queue_id,
            )
            repeated = import_reference_render_preference(
                fresh_audit_root,
                comparison.directory,
                session,
                queue_id,
            )
            decisions = load_failure_reference_decisions(fresh_audit_root)
            binding_root = root / "binding"
            publish_failure_reference_binding(fresh_audit_root, binding_root)
            binding = load_failure_reference_binding_document(binding_root)
            self.assertEqual(
                authoring_main(
                    [
                        "failure-reference-import-listening",
                        str(fresh_audit_root),
                        str(comparison.directory),
                        str(session),
                        queue_id,
                    ]
                ),
                0,
            )

            self.assertTrue(imported.created)
            self.assertFalse(repeated.created)
            self.assertEqual(imported.audit_id, fresh_audit.audit_id)
            self.assertEqual(imported.decision_set_id, repeated.decision_set_id)
            self.assertEqual(
                imported.selected_reference_sha256,
                selected_reference_sha256,
            )
            authority = decisions["decisions"][0]["selection_authority"]
            self.assertEqual(authority["selected_arm_id"], selected_arm)
            self.assertEqual(authority["queue_id"], queue_id)
            self.assertEqual(
                authority["selected_render_sha256"],
                selected_assignment["audio_sha256"],
            )
            self.assertEqual(binding["groups"][0]["selection_authority"], authority)

    def test_import_rejects_tie_and_stale_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            audit_root = root / "audit"
            audit = publish_failure_reference_audit(
                workspace, audit_root, seed=0, queue_ids=(queue_id,)
            )
            document = json.loads((audit_root / "audit.json").read_text())
            group = document["groups"][0]
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                        "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                        "audit": str(audit_root),
                        "audit_id": audit.audit_id,
                        "arms": [
                            {
                                "arm_id": f"reference-{index}",
                                "samples": [
                                    {
                                        "queue_id": queue_id,
                                        "case_group_id": group["group_id"],
                                        "candidate_group_id": group["group_id"],
                                        "candidate_id": candidate["candidate_id"],
                                    }
                                ],
                            }
                            for index, candidate in enumerate(
                                group["candidates"], start=1
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            comparison = publish_reference_render_comparison(
                load_reference_render_plan(plan_path),
                root / "comparison",
                backend_factory=_PreviewBackendFactory(),
            )
            session = create_reference_render_listening(
                comparison.directory, root / "listening", seed=7
            )
            trial_id = json.loads(session.read_text())["trials"][0]["trial_id"]
            record_trial_preference(
                session,
                trial_id,
                "tie",
                report_path=session.with_name("report.json"),
            )
            fresh_audit = root / "fresh-audit"
            publish_failure_reference_audit(
                workspace, fresh_audit, seed=19, queue_ids=(queue_id,)
            )
            with self.assertRaisesRegex(
                ReferenceRenderComparisonError, "did not select"
            ):
                import_reference_render_preference(
                    fresh_audit, comparison.directory, session, queue_id
                )

            record_trial_preference(
                session,
                trial_id,
                "a",
                overwrite=True,
                report_path=session.with_name("report.json"),
            )
            report = json.loads(session.with_name("report.json").read_text())
            report["completed_trials"] = 0
            session.with_name("report.json").write_text(json.dumps(report))
            with self.assertRaisesRegex(
                ReferenceRenderComparisonError, "report is stale"
            ):
                import_reference_render_preference(
                    fresh_audit, comparison.directory, session, queue_id
                )

    def test_render_plan_rejects_duplicate_or_cross_character_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            audit_root = root / "audit"
            audit = publish_failure_reference_audit(workspace, audit_root)
            document = json.loads((audit_root / "audit.json").read_text())
            group = document["groups"][0]
            candidate = group["candidates"][0]
            plan = {
                "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                "audit": str(audit_root),
                "audit_id": audit.audit_id,
                "arms": [
                    {
                        "arm_id": arm_id,
                        "samples": [
                            {
                                "queue_id": queue_id,
                                "case_group_id": group["group_id"],
                                "candidate_group_id": group["group_id"],
                                "candidate_id": candidate["candidate_id"],
                            }
                        ],
                    }
                    for arm_id in ("one", "two")
                ],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with self.assertRaisesRegex(
                ReferenceRenderComparisonError, "repeat the same control"
            ):
                load_reference_render_plan(plan_path)


if __name__ == "__main__":
    unittest.main()
