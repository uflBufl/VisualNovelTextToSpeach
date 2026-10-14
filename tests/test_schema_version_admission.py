import json
import unittest
from collections import UserDict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.authoring_fixtures import _legacy_bad_fixture
from vntts.authoring import game_pack, speaker_identity
from vntts.authoring.config_rebase import _failure_reference_route
from vntts.authoring.game_pack import _PublicationLease
from vntts.authoring.generation_lease import LEASE_SCHEMA
from vntts.authoring.legacy_reason_review import (
    LegacyReasonReviewError,
    build_legacy_reason_review,
    load_reason_review_progress,
    write_reason_review_progress,
)
from vntts.authoring.missing_voice_live_fallback import (
    MISSING_VOICE_REUSE_DECISION_SCHEMA,
    MissingVoiceLiveFallbackError,
    _load_authority,
)
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.missing_voice_reuse import (
    MISSING_VOICE_REUSE_PLAN_SCHEMA,
    MISSING_VOICE_REUSE_PLAN_VERSION,
    MissingVoiceReuseError,
    _validate_plan_header,
)
from vntts.authoring.speaker_identity import (
    INVENTORY_SCHEMA,
    LABELS_SCHEMA,
    SCHEMA_VERSION,
    SpeakerIdentityError,
    _validate_inventory_shape,
    _validate_labels_shape,
)
from vntts.authoring.workbench_contracts import AuthoringWorkbenchError
from vntts.authoring.workspace_inspection import (
    AuthoringRuntimeStatus,
    _leased_runtime_status,
)
from vntts.document_identity import canonical_document_sha256
from vntts.json_types import has_schema_version
from vntts.pregeneration_voices import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    PLAYER_VOICE_CANDIDATES_VERSION,
    player_voice_catalog_is_current,
)


class SchemaVersionAdmissionTest(unittest.TestCase):
    def test_shared_predicate_keeps_mapping_support_and_exact_integer_semantics(self):
        self.assertTrue(has_schema_version(UserDict(schema_version=1), 1))
        self.assertFalse(has_schema_version(UserDict(schema_version=True), 1))
        self.assertFalse(has_schema_version(UserDict(schema_version=1.0), 1))

    def test_policy_rejects_boolean_and_float_versions(self):
        for version in (True, 1.0):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    MissingVoicePolicyError, "Unsupported missing-voice policy version"
                ),
            ):
                MissingVoicePolicy.from_document(
                    {"schema_version": version, "mode": "block", "roles": []}
                )

    def test_reason_progress_rejects_boolean_and_float_versions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            progress = root / "progress.json"
            write_reason_review_progress(review, progress, {})
            for version in (True, 1.0):
                document = json.loads(progress.read_text(encoding="utf-8"))
                document["schema_version"] = version
                progress.write_text(json.dumps(document), encoding="utf-8")
                with (
                    self.subTest(version=version),
                    self.assertRaisesRegex(
                        LegacyReasonReviewError, "different corpus evidence"
                    ),
                ):
                    load_reason_review_progress(review, progress)

    def test_speaker_inventory_and_labels_reject_non_integer_versions(self):
        inventory = {
            "schema": INVENTORY_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "voice_manifest": "voices.json",
            "voice_manifest_sha256": "0" * 64,
            "reference_count": 0,
            "references": [],
        }
        inventory["inventory_id"] = speaker_identity._document_sha256(inventory)
        labels = {
            "schema": LABELS_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "inventory_id": inventory["inventory_id"],
            "pairs": [],
        }
        labels["labels_id"] = speaker_identity._document_sha256(labels)
        _validate_inventory_shape(inventory)
        _validate_labels_shape(labels)

        for version in (True, 1.0):
            invalid_inventory = {**inventory, "schema_version": version}
            invalid_inventory["inventory_id"] = speaker_identity._document_sha256(
                {
                    key: value
                    for key, value in invalid_inventory.items()
                    if key != "inventory_id"
                }
            )
            invalid_labels = {**labels, "schema_version": version}
            invalid_labels["labels_id"] = speaker_identity._document_sha256(
                {
                    key: value
                    for key, value in invalid_labels.items()
                    if key != "labels_id"
                }
            )
            with self.subTest(document="inventory", version=version):
                with self.assertRaisesRegex(SpeakerIdentityError, "Unsupported"):
                    _validate_inventory_shape(invalid_inventory)
            with self.subTest(document="labels", version=version):
                with self.assertRaisesRegex(SpeakerIdentityError, "Unsupported"):
                    _validate_labels_shape(invalid_labels)

    def test_missing_voice_reuse_header_rejects_non_integer_version(self):
        document = {
            "schema": MISSING_VOICE_REUSE_PLAN_SCHEMA,
            "schema_version": MISSING_VOICE_REUSE_PLAN_VERSION,
        }
        document["plan_id"] = canonical_document_sha256(document)
        _validate_plan_header(document)
        for version in (True, 1.0):
            invalid = {**document, "schema_version": version}
            invalid["plan_id"] = canonical_document_sha256(
                {key: value for key, value in invalid.items() if key != "plan_id"}
            )
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(MissingVoiceReuseError, "schema is unsupported"),
            ):
                _validate_plan_header(invalid)

    def test_player_catalog_current_requires_exact_integer_version(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for version, expected in (
                (PLAYER_VOICE_CANDIDATES_VERSION, True),
                (True, False),
                (float(PLAYER_VOICE_CANDIDATES_VERSION), False),
            ):
                path.write_text(
                    json.dumps(
                        {PLAYER_VOICE_CANDIDATES_FIELD: {"schema_version": version}}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(player_voice_catalog_is_current(path), expected)

    def test_missing_voice_fallback_decision_rejects_non_integer_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for version in (True, 1.0):
                decision = {
                    "schema": MISSING_VOICE_REUSE_DECISION_SCHEMA,
                    "schema_version": version,
                    "plan_path": "plan.json",
                    "plan_sha256": "0" * 64,
                    "session_path": "session.json",
                    "binding": {},
                }
                decision["decision_id"] = canonical_document_sha256(decision)
                with (
                    self.subTest(version=version),
                    patch(
                        "vntts.authoring.missing_voice_live_fallback._read_json",
                        return_value=decision,
                    ),
                    self.assertRaisesRegex(
                        MissingVoiceLiveFallbackError,
                        "decision identity is invalid",
                    ),
                ):
                    _load_authority(root)

    def test_config_rebase_failure_binding_rejects_non_integer_version(self):
        queue_id = "line:historical-reference"
        result = {
            "voice_character": "Selected failure reference exact",
            "source_reference_binding": {
                "schema_version": 1,
                "queue_id": queue_id,
                "source_voice_character": "Narrator",
                "synthesis_voice_character": "Selected failure reference exact",
                "queue_voice_overrides_sha256": "1" * 64,
            },
            "failure_repair": {
                "schema_version": 1,
                "strategy": "offline_fallback_backend",
                "source_failure": {
                    "source_voice_reference": {
                        "character": "Selected failure reference exact",
                        "speaker": "failure-reference:exact",
                        "aliases": [],
                        "references": ["3" * 64, "2" * 64],
                    }
                },
            },
        }
        from types import SimpleNamespace

        item = SimpleNamespace(queue_id=queue_id)
        self.assertIsNotNone(_failure_reference_route(None, item, result))
        for version in (True, 1.0):
            result["source_reference_binding"]["schema_version"] = version
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    AuthoringWorkbenchError, "failure-reference binding changed"
                ),
            ):
                _failure_reference_route(None, item, result)

    def test_inspector_blocks_boolean_and_float_lease_versions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lease.json"
            for version in (True, 1.0):
                path.write_text(
                    json.dumps(
                        {
                            "schema": LEASE_SCHEMA,
                            "schema_version": version,
                            "queue_sha256": "0" * 64,
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    _leased_runtime_status(
                        path,
                        queue_sha256="0" * 64,
                        local_process_id=None,
                        local_process_started_at=None,
                        process_checker=lambda _pid: True,
                        process_start_checker=lambda _pid: "start",
                    ),
                    AuthoringRuntimeStatus.BLOCKED,
                )

    def test_publication_lease_keeps_malformed_version_conservatively_live(self):
        with TemporaryDirectory() as directory:
            lease = _PublicationLease(Path(directory) / "final-pack")
            valid = {
                "schema": "vntts.game-pack-publication-lease",
                "schema_version": 1,
                "owner": "owner",
                "destination": str(lease.destination),
                "hostname": game_pack.socket.gethostname(),
                "pid": 999999,
                "process_started_at": "start",
            }
            with patch.object(game_pack, "process_is_alive", return_value=False):
                self.assertFalse(lease._existing_is_live(valid))
                for version in (True, 1.0):
                    with self.subTest(version=version):
                        self.assertTrue(
                            lease._existing_is_live(
                                {**valid, "schema_version": version}
                            )
                        )


if __name__ == "__main__":
    unittest.main()
