import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_BINDING_FIELD,
    MISSING_VOICE_REUSE_BINDING_SCHEMA,
    MISSING_VOICE_REUSE_BINDING_VERSION,
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SOURCE_REFERENCE_BINDINGS_SCHEMA,
    SOURCE_REFERENCE_BINDINGS_VERSION,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)


class AuthoringSourceReferenceBindingsTest(unittest.TestCase):
    def document(self):
        source_overrides = {"queue:source": "Source variant"}
        reuse_overrides = {"queue:reuse": "Centurion"}
        return {
            SOURCE_REFERENCE_BINDINGS_FIELD: {
                "schema": SOURCE_REFERENCE_BINDINGS_SCHEMA,
                "schema_version": SOURCE_REFERENCE_BINDINGS_VERSION,
                "source_reference_plan_sha256": "1" * 64,
                "selected_variants": [
                    {
                        "variant_id": "source-variant",
                        "voice_character": "Source variant",
                    }
                ],
                "queue_voice_overrides": source_overrides,
                "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
                    source_overrides
                ),
            },
            MISSING_VOICE_REUSE_BINDING_FIELD: {
                "schema": MISSING_VOICE_REUSE_BINDING_SCHEMA,
                "schema_version": MISSING_VOICE_REUSE_BINDING_VERSION,
                "mode": "comparison_sample_only",
                "plan_id": "2" * 64,
                "candidate_id": "3" * 64,
                "source_voice_manifest_sha256": "4" * 64,
                "source_workspace_id": "resume-source",
                "source_workspace_sha256": "5" * 64,
                "candidate_voice_character": "Centurion",
                "candidate_reference_sha256s": ["6" * 64],
                "cohort_ids": ["7" * 64],
                "queue_voice_overrides": reuse_overrides,
                "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
                    reuse_overrides
                ),
                "authority": (
                    "Comparison-only exact sample bindings. This authority does not "
                    "bind the remaining cohort or approve generated audio."
                ),
            },
        }

    def test_generator_queue_scope_is_shared_by_both_binding_layers(self):
        document = self.document()
        voices = (
            SimpleNamespace(character="Source variant"),
            SimpleNamespace(character="Centurion"),
        )

        overrides = queue_voice_overrides_from_manifest(
            document,
            queue_ids=(value for value in ("queue:source", "queue:reuse")),
            voices=voices,
        )

        self.assertEqual(
            overrides,
            {"queue:source": "Source variant", "queue:reuse": "Centurion"},
        )

    def test_binding_layers_cannot_overlap_queue_ids(self):
        document = self.document()
        document = copy.deepcopy(document)
        reuse = document[MISSING_VOICE_REUSE_BINDING_FIELD]
        reuse["queue_voice_overrides"] = {"queue:source": "Centurion"}
        reuse["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
            reuse["queue_voice_overrides"]
        )

        with self.assertRaisesRegex(SourceReferenceBindingError, "overlap"):
            queue_voice_overrides_from_manifest(
                document,
                queue_ids=("queue:source",),
                voices=(
                    SimpleNamespace(character="Source variant"),
                    SimpleNamespace(character="Centurion"),
                ),
            )

    def test_exact_failed_control_can_be_superseded_by_review_candidate(self):
        document = copy.deepcopy(self.document())
        reuse = document[MISSING_VOICE_REUSE_BINDING_FIELD]
        reuse["queue_voice_overrides"] = {"queue:source": "Centurion"}
        reuse["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
            reuse["queue_voice_overrides"]
        )
        reuse["target_mode"] = "failed"
        reuse["source_failed_state_item_sha256s"] = {"queue:source": "8" * 64}

        overrides = queue_voice_overrides_from_manifest(
            document,
            queue_ids=("queue:source",),
            voices=(
                SimpleNamespace(character="Source variant"),
                SimpleNamespace(character="Centurion"),
            ),
        )

        self.assertEqual(overrides, {"queue:source": "Centurion"})

    def test_failed_overlap_requires_every_exact_source_item_hash(self):
        document = copy.deepcopy(self.document())
        reuse = document[MISSING_VOICE_REUSE_BINDING_FIELD]
        reuse["queue_voice_overrides"] = {"queue:source": "Centurion"}
        reuse["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
            reuse["queue_voice_overrides"]
        )
        reuse["target_mode"] = "failed"
        reuse["source_failed_state_item_sha256s"] = {}

        with self.assertRaisesRegex(SourceReferenceBindingError, "overlap"):
            queue_voice_overrides_from_manifest(
                document,
                queue_ids=("queue:source",),
                voices=(
                    SimpleNamespace(character="Source variant"),
                    SimpleNamespace(character="Centurion"),
                ),
            )

    def test_exact_failed_comparison_can_repeat_same_known_role_route(self):
        document = self.document()
        document = copy.deepcopy(document)
        reuse = document[MISSING_VOICE_REUSE_BINDING_FIELD]
        reuse["target_mode"] = "failed"
        reuse["source_failed_state_item_sha256s"] = {"queue:reuse": "8" * 64}

        with patch(
            "vntts.authoring.source_reference_bindings."
            "_known_role_reuse_overrides_from_manifest",
            return_value={"queue:reuse": "centurion"},
        ):
            overrides = queue_voice_overrides_from_manifest(
                document,
                queue_ids=("queue:source", "queue:reuse"),
                voices=(
                    SimpleNamespace(character="Source variant"),
                    SimpleNamespace(character="Centurion"),
                ),
            )

        self.assertEqual(overrides["queue:reuse"], "centurion")

    def test_failed_comparison_cannot_replace_known_role_with_another_voice(self):
        document = self.document()
        document = copy.deepcopy(document)
        reuse = document[MISSING_VOICE_REUSE_BINDING_FIELD]
        reuse["target_mode"] = "failed"
        reuse["source_failed_state_item_sha256s"] = {"queue:reuse": "8" * 64}

        with patch(
            "vntts.authoring.source_reference_bindings."
            "_known_role_reuse_overrides_from_manifest",
            return_value={"queue:reuse": "Rhiannon"},
        ):
            with self.assertRaisesRegex(SourceReferenceBindingError, "overlap"):
                queue_voice_overrides_from_manifest(
                    document,
                    queue_ids=("queue:source", "queue:reuse"),
                    voices=(
                        SimpleNamespace(character="Source variant"),
                        SimpleNamespace(character="Centurion"),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
