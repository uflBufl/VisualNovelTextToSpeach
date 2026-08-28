import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.offline_fallback_authority import (
    OfflineFallbackAuthorityError,
    load_offline_fallback_authorities,
    validate_offline_fallback_authority_records,
)


def write_authority(path, queue_id, source_item_sha256, *, kind="voice", origin=None):
    origin = origin or "automatic_no_complete_candidate"
    if kind == "voice":
        body = {
            "schema": "vntts.authoring-missing-voice-reuse-decision",
            "schema_version": 1,
            "binding": {
                "target_mode": "failed",
                "queue_voice_overrides": {},
                "selected_candidates": [],
                "decisions": [
                    {
                        "decision": "neither",
                        "review_decision_origin": origin,
                        "queue_ids": [queue_id],
                    }
                ],
                "source_failed_state_item_sha256s": {queue_id: source_item_sha256},
            },
        }
        document = {**body, "decision_id": _canonical_sha256(body)}
    else:
        body = {
            "schema": "vntts.authoring-failed-prompt-selection",
            "schema_version": 1,
            "decisions": [
                {
                    "decision": "keep_unresolved",
                    "review_decision_origin": origin,
                    "queue_ids": [queue_id],
                    "source_state_item_sha256s": {queue_id: source_item_sha256},
                }
            ],
        }
        document = {**body, "selection_id": _canonical_sha256(body)}
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


class AuthoringOfflineFallbackAuthorityTest(unittest.TestCase):
    def test_accepts_both_canonical_automatic_unresolved_artifacts(self):
        queue_ids = ("queue:1", "queue:2")
        source_items = {
            queue_id: {"status": "failed", "attempts": index}
            for index, queue_id in enumerate(queue_ids, 1)
        }
        hashes = {
            queue_id: _canonical_sha256(item) for queue_id, item in source_items.items()
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                write_authority(
                    root / "voice.json", queue_ids[0], hashes[queue_ids[0]]
                ),
                write_authority(
                    root / "prompt.json",
                    queue_ids[1],
                    hashes[queue_ids[1]],
                    kind="prompt",
                ),
            )
            loaded = load_offline_fallback_authorities(paths, source_items, queue_ids)
            snapshots = []
            snapshot_root = root / "snapshots"
            snapshot_root.mkdir()
            for authority in loaded:
                target = snapshot_root / f"{authority.authority_id}.json"
                target.write_bytes(authority.payload)
                snapshots.append(authority.snapshot_record(target.name))
            validated = validate_offline_fallback_authority_records(
                snapshots,
                snapshot_root,
                hashes,
            )

        self.assertEqual(
            {value.kind for value in validated},
            {"failed_voice_review", "failed_prompt_review"},
        )

    def test_rejects_human_selected_stale_unrelated_and_tampered_authority(self):
        queue_id = "queue:1"
        source_item = {"status": "failed", "attempts": 1}
        source_hash = _canonical_sha256(source_item)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            human = write_authority(
                root / "human.json",
                queue_id,
                source_hash,
                origin="human_review",
            )
            with self.assertRaisesRegex(
                OfflineFallbackAuthorityError, "not automatically unresolved"
            ):
                load_offline_fallback_authorities(
                    (human,), {queue_id: source_item}, (queue_id,)
                )

            stale = write_authority(root / "stale.json", queue_id, "0" * 64)
            with self.assertRaisesRegex(OfflineFallbackAuthorityError, "stale"):
                load_offline_fallback_authorities(
                    (stale,), {queue_id: source_item}, (queue_id,)
                )

            exact = write_authority(root / "exact.json", queue_id, source_hash)
            with self.assertRaisesRegex(
                OfflineFallbackAuthorityError, "cover every selected queue ID"
            ):
                load_offline_fallback_authorities(
                    (exact,), {queue_id: source_item}, (queue_id, "queue:2")
                )

            document = json.loads(exact.read_text(encoding="utf-8"))
            document["binding"]["selected_candidates"] = [{"candidate_id": "bad"}]
            document["decision_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "decision_id"}
            )
            exact.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(
                OfflineFallbackAuthorityError, "zero-override decision"
            ):
                load_offline_fallback_authorities(
                    (exact,), {queue_id: source_item}, (queue_id,)
                )

    def test_snapshot_validation_rejects_duplicate_and_changed_copy(self):
        queue_id = "queue:1"
        source_item = {"status": "failed", "attempts": 1}
        source_hash = _canonical_sha256(source_item)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_authority(root / "source.json", queue_id, source_hash)
            authority = load_offline_fallback_authorities(
                (source,), {queue_id: source_item}, (queue_id,)
            )[0]
            copy_path = root / "copy.json"
            copy_path.write_bytes(authority.payload)
            record = authority.snapshot_record(copy_path.name)
            with self.assertRaisesRegex(OfflineFallbackAuthorityError, "duplicated"):
                validate_offline_fallback_authority_records(
                    [record, copy.deepcopy(record)], root, {queue_id: source_hash}
                )
            second_source = write_authority(
                root / "prompt-source.json",
                queue_id,
                source_hash,
                kind="prompt",
            )
            second = load_offline_fallback_authorities(
                (second_source,), {queue_id: source_item}, (queue_id,)
            )[0]
            second_copy = root / "prompt-copy.json"
            second_copy.write_bytes(second.payload)
            with self.assertRaisesRegex(OfflineFallbackAuthorityError, "overlap"):
                validate_offline_fallback_authority_records(
                    [record, second.snapshot_record(second_copy.name)],
                    root,
                    {queue_id: source_hash},
                )
            copy_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(OfflineFallbackAuthorityError):
                validate_offline_fallback_authority_records(
                    [record], root, {queue_id: source_hash}
                )


if __name__ == "__main__":
    unittest.main()
