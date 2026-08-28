import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests.test_authoring_missing_voice_reuse import AuthoringMissingVoiceReuseTest
from vntts.authoring.missing_voice_reuse import write_missing_voice_reuse_plan
from vntts.authoring.missing_voice_reuse_review import (
    AUTOMATIC_UNRESOLVED_ORIGIN,
    MissingVoiceReuseReviewError,
    build_missing_voice_reuse_review,
    load_missing_voice_reuse_review,
    missing_voice_reuse_review_progress,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 800)


class AuthoringMissingVoiceReuseReviewTest(unittest.TestCase):
    def fixture(self, root, statuses=("generated", "failed")):
        helper = AuthoringMissingVoiceReuseTest()
        fixture, _imported, workspace = helper.create_workspace(root)
        plan = helper.build_plan(workspace)
        plan_path = root / "plan.json"
        write_missing_voice_reuse_plan(plan, plan_path)
        queue_id = fixture["queue_id"]
        snapshots = {}
        evidence = {}
        for index, (candidate, status) in enumerate(
            zip(plan.document["candidates"], statuses, strict=True), start=1
        ):
            candidate_root = root / f"candidate-{index}"
            candidate_root.mkdir()
            evidence[candidate["candidate_id"]] = (candidate_root,)
            item = {
                "status": status,
                "attempts": 1,
                "source_reference_binding": {
                    "queue_id": queue_id,
                    "synthesis_voice_character": candidate["voice_character"],
                },
            }
            if status == "generated":
                audio = candidate_root / "generated-audio/audio/sample.wav"
                write_wav(audio)
                item.update(
                    {
                        "path": "audio/sample.wav",
                        "file_sha256": sha256_file(audio),
                        "quality": {"duration_seconds": 0.1},
                    }
                )
            else:
                item.update(
                    {
                        "failure": {"kind": "missed_eos_audio_limit"},
                        "last_error": "Typed limited render",
                    }
                )
            snapshots[candidate_root.resolve()] = {
                "directory": candidate_root.resolve(),
                "workspace": {"workspace_id": f"workspace-{index}"},
                "state": {"items": {queue_id: item}},
                "authority": {
                    "path": str(candidate_root.resolve()),
                    "workspace_id": f"workspace-{index}",
                    "workspace_sha256": f"{index}" * 64,
                    "state_sha256": f"{index + 2}" * 64,
                    "voice_manifest_sha256": f"{index + 4}" * 64,
                },
            }
        return plan_path, evidence, snapshots, queue_id

    def test_failed_arm_stays_visible_and_cannot_be_selected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, evidence, snapshots, queue_id = self.fixture(root)
            with patch(
                "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
                side_effect=lambda _plan, _candidate, path: snapshots[
                    Path(path).resolve()
                ],
            ):
                session_path = build_missing_voice_reuse_review(
                    plan_path, evidence, root / "review", seed=7
                )
            bundle, session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            generated = next(
                candidate
                for candidate in bundle["candidates"]
                if candidate["samples"][0]["status"] == "generated"
            )
            failed = next(
                candidate
                for candidate in bundle["candidates"]
                if candidate["samples"][0]["status"] == "failed"
            )

            self.assertEqual(cohort["complete_candidate_labels"], [generated["label"]])
            self.assertEqual(
                cohort["decision_options"], [generated["label"], "neither"]
            )
            self.assertEqual(
                failed["samples"][0]["failure_kind"], "missed_eos_audio_limit"
            )
            with self.assertRaisesRegex(
                MissingVoiceReuseReviewError, "cannot be heard"
            ):
                record_missing_voice_reuse_heard(
                    session_path, cohort["cohort_id"], queue_id, failed["label"]
                )
            with self.assertRaisesRegex(MissingVoiceReuseReviewError, "must be heard"):
                record_missing_voice_reuse_decision(
                    session_path, cohort["cohort_id"], generated["label"]
                )
            record_missing_voice_reuse_heard(
                session_path, cohort["cohort_id"], queue_id, generated["label"]
            )
            updated = record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], generated["label"]
            )

            self.assertEqual(
                missing_voice_reuse_review_progress(bundle, session), (0, 1)
            )
            self.assertEqual(updated["decisions"][0]["decision"], generated["label"])
            self.assertEqual(
                load_missing_voice_reuse_review(session_path)[1]["decisions"][0][
                    "decision"
                ],
                generated["label"],
            )

    def test_all_failed_cohort_is_automatically_unresolved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, evidence, snapshots, _queue_id = self.fixture(
                root, statuses=("failed", "failed")
            )
            with patch(
                "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
                side_effect=lambda _plan, _candidate, path: snapshots[
                    Path(path).resolve()
                ],
            ):
                session_path = build_missing_voice_reuse_review(
                    plan_path, evidence, root / "review"
                )
            bundle, session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]

            self.assertEqual(cohort["complete_candidate_labels"], [])
            self.assertEqual(cohort["decision_options"], ["neither"])
            self.assertEqual(
                session["decisions"][0],
                {
                    "cohort_id": cohort["cohort_id"],
                    "decision": "neither",
                    "decided_at": session["created_at"],
                    "decision_origin": AUTOMATIC_UNRESOLVED_ORIGIN,
                },
            )
            self.assertEqual(
                missing_voice_reuse_review_progress(bundle, session), (1, 1)
            )
            with self.assertRaisesRegex(
                MissingVoiceReuseReviewError, "already has a decision"
            ):
                record_missing_voice_reuse_decision(
                    session_path, cohort["cohort_id"], "neither"
                )

    def test_legacy_pending_zero_choice_cohort_is_projected_as_unresolved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, evidence, snapshots, _queue_id = self.fixture(
                root, statuses=("failed", "failed")
            )
            with patch(
                "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
                side_effect=lambda _plan, _candidate, path: snapshots[
                    Path(path).resolve()
                ],
            ):
                session_path = build_missing_voice_reuse_review(
                    plan_path, evidence, root / "review"
                )
            raw = json.loads(session_path.read_text(encoding="utf-8"))
            cohort_id = raw["decisions"][0]["cohort_id"]
            raw["decisions"] = [{"cohort_id": cohort_id, "decision": None}]
            session_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

            bundle, session = load_missing_voice_reuse_review(session_path)

        self.assertEqual(session["decisions"][0]["decision"], "neither")
        self.assertEqual(
            session["decisions"][0]["decision_origin"],
            AUTOMATIC_UNRESOLVED_ORIGIN,
        )
        self.assertEqual(missing_voice_reuse_review_progress(bundle, session), (1, 1))

    def test_exact_failed_control_can_review_one_complete_alternative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = AuthoringMissingVoiceReuseTest()
            fixture, _imported, workspace = helper.create_workspace(root)
            plan = helper.build_failed_plan(fixture, workspace)
            plan_path = root / "failed-plan.json"
            write_missing_voice_reuse_plan(plan, plan_path)
            candidate = plan.document["candidates"][0]
            candidate_root = (root / "candidate").resolve()
            candidate_root.mkdir()
            audio = candidate_root / "generated-audio/audio/sample.wav"
            write_wav(audio)
            queue_id = fixture["queue_id"]
            snapshot = {
                "directory": candidate_root,
                "workspace": {"workspace_id": "candidate-workspace"},
                "state": {
                    "items": {
                        queue_id: {
                            "status": "generated",
                            "attempts": 1,
                            "path": "audio/sample.wav",
                            "file_sha256": sha256_file(audio),
                            "quality": {"duration_seconds": 0.1},
                            "source_reference_binding": {
                                "queue_id": queue_id,
                                "synthesis_voice_character": candidate[
                                    "voice_character"
                                ],
                            },
                        }
                    }
                },
                "authority": {
                    "path": str(candidate_root),
                    "workspace_id": "candidate-workspace",
                    "workspace_sha256": "1" * 64,
                    "state_sha256": "2" * 64,
                    "voice_manifest_sha256": "3" * 64,
                },
            }
            with patch(
                "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
                return_value=snapshot,
            ):
                session_path = build_missing_voice_reuse_review(
                    plan_path,
                    {candidate["candidate_id"]: (candidate_root,)},
                    root / "review",
                )
            bundle, _session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            label = bundle["candidates"][0]["label"]
            record_missing_voice_reuse_heard(
                session_path, cohort["cohort_id"], queue_id, label
            )
            decided = record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], label
            )

        self.assertEqual(bundle["candidate_count"], 1)
        self.assertEqual(bundle["target_mode"], "failed")
        self.assertEqual(
            bundle["source_control"],
            [
                {
                    "queue_id": queue_id,
                    "status": "failed",
                    "failure_category": "speech silence",
                    "state_item_sha256": plan.document["targets"][0][
                        "source_state_item_sha256"
                    ],
                }
            ],
        )
        self.assertEqual(cohort["decision_options"], [label, "neither"])
        self.assertEqual(decided["decisions"][0]["decision"], label)

    def test_review_audio_and_bundle_are_tamper_evident(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, evidence, snapshots, _queue_id = self.fixture(root)
            with patch(
                "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
                side_effect=lambda _plan, _candidate, path: snapshots[
                    Path(path).resolve()
                ],
            ):
                session_path = build_missing_voice_reuse_review(
                    plan_path, evidence, root / "review"
                )
            bundle, _session = load_missing_voice_reuse_review(session_path)
            generated = next(
                sample
                for candidate in bundle["candidates"]
                for sample in candidate["samples"]
                if sample["status"] == "generated"
            )
            (session_path.parent / generated["audio"]).write_bytes(b"changed")

            with self.assertRaisesRegex(MissingVoiceReuseReviewError, "audio changed"):
                load_missing_voice_reuse_review(session_path)


if __name__ == "__main__":
    unittest.main()
