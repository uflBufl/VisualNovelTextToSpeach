import unittest
from typing import cast

from vntts.authoring.bulk_generation import BulkGenerationError
from vntts.authoring.generation_state import (
    _validate_reviewed_rejection_live_fallback_evidence,
    _validate_reviewed_waveform_publication_metadata,
    _validate_reviewed_waveform_route,
)
from vntts.document_identity import canonical_document_sha256
from vntts.generated_audio import _validate_reviewed_rejection_fallback_evidence


def _reviewed_rejection_evidence(
    references: list[object], *, rebase_references: list[object] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "generated",
        "review_status": "rejected",
    }
    if rebase_references is not None:
        result["config_rebase"] = {
            "target_route_status": "active",
            "target_effective_character": "Synthesis",
            "target_reference_sha256s": rebase_references,
        }
    result_sha256 = canonical_document_sha256(result)
    return {
        "schema": "vntts.authoring-reviewed-rejection-live-fallback-evidence",
        "schema_version": 1,
        "batch_id": "a" * 64,
        "base_workspace_id": "workspace",
        "base_workspace_sha256": "b" * 64,
        "base_state_sha256": "c" * 64,
        "queue_sha256": "d" * 64,
        "voice_manifest_sha256": "e" * 64,
        "queue_id": "queue",
        "base_result_sha256": result_sha256,
        "base_result": result,
        "source_character": "Source",
        "synthesis_character": "Synthesis",
        "route_source": "config_rebase"
        if rebase_references is not None
        else "voice_manifest",
        "route_reference_sha256s": references,
    }


class CanonicalListBoundaryTests(unittest.TestCase):
    def test_generation_state_rejects_non_text_canonical_lists(self) -> None:
        valid_reference = "f" * 64
        publication: dict[str, object] = {
            "batch_id": "a" * 64,
            "base_workspace_sha256": "b" * 64,
            "base_state_sha256": "c" * 64,
            "queue_sha256": "d" * 64,
            "selected_story_index_sha256": "e" * 64,
            "selected_voice_manifest_sha256": "f" * 64,
            "base_workspace_id": "workspace",
            "base_workspace_path": "workspace.json",
            "base_state_path": "generation-state.json",
            "narrator_character": "Narrator",
            "narrator_reference_sha256s": [{}],
        }
        route = {
            "source": "historical_reviewed_waveform",
            "status": "not_reproducible",
            "effective_character": "Narrator",
            "reference_sha256s": [{}],
        }

        with self.assertRaisesRegex(BulkGenerationError, "narrator references"):
            _validate_reviewed_waveform_publication_metadata(publication)
        with self.assertRaisesRegex(
            BulkGenerationError, "references are not canonical"
        ):
            _validate_reviewed_waveform_route(route, "queue")
        evidence = _reviewed_rejection_evidence([{}])
        with self.assertRaisesRegex(BulkGenerationError, "evidence is malformed"):
            _validate_reviewed_rejection_live_fallback_evidence(
                evidence,
                "queue",
                evidence["base_result_sha256"],
                "Synthesis",
                None,
            )
        evidence = _reviewed_rejection_evidence(
            [valid_reference], rebase_references=[{}]
        )
        with self.assertRaisesRegex(BulkGenerationError, "route changed"):
            _validate_reviewed_rejection_live_fallback_evidence(
                evidence,
                "queue",
                evidence["base_result_sha256"],
                "Synthesis",
                None,
            )

    def test_generated_audio_rejects_non_text_references(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence is malformed"):
            evidence = _reviewed_rejection_evidence([{}])
            _validate_reviewed_rejection_fallback_evidence(
                evidence,
                "queue",
                "Source",
                "Synthesis",
                cast(str, evidence["base_result_sha256"]),
            )
        evidence = _reviewed_rejection_evidence(["f" * 64], rebase_references=[{}])
        with self.assertRaisesRegex(ValueError, "config route changed"):
            _validate_reviewed_rejection_fallback_evidence(
                evidence,
                "queue",
                "Source",
                "Synthesis",
                cast(str, evidence["base_result_sha256"]),
            )


if __name__ == "__main__":
    unittest.main()
