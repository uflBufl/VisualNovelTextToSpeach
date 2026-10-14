import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_manifest import VoiceManifestEntry, VoiceManifestError

from vntts import live_replay, pregeneration_voices, voices
from vntts.authoring import (
    cohort_bundle,
    legacy_import,
    listening,
    offline_fallback_authority,
    queue_builder,
    reference_render_comparison,
    robustness_corpus,
    source_reference_quality,
    terminal_conflict_successor,
    voice_repair_comparison,
)
from vntts.document_identity import canonical_document_sha256


class RelativePathBoundariesTest(unittest.TestCase):
    def test_listening_rejects_nul_with_its_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                listening.ModelListeningError, "POSIX-relative"
            ):
                listening._within(root, "audio/bad\x00.wav", "audio")
            self.assertEqual(
                listening._within(root, " clip.wav ", "audio"),
                (root / " clip.wav ").resolve(),
            )

    def test_robustness_rejects_nul_without_changing_relative_names(self):
        with self.assertRaisesRegex(
            robustness_corpus.SpeechRobustnessCorpusError, "POSIX-relative"
        ):
            robustness_corpus._relative("audio/bad\x00.wav", "audio")
        for value in (" clip.wav ", "C:clip.wav"):
            self.assertEqual(robustness_corpus._relative(value, "audio"), Path(value))

    def test_queue_references_reject_nul_and_keep_the_colon_policy(self):
        with TemporaryDirectory() as directory:
            for reference, message in (
                ("audio/bad\x00.wav", "POSIX-relative"),
                ("bank:clip.wav", "manifest directory"),
            ):
                with self.subTest(reference=reference):
                    entry = VoiceManifestEntry("Voice", "voice", (), (reference,))
                    with self.assertRaisesRegex(
                        queue_builder.GenerationQueueBuildError, message
                    ):
                        queue_builder._local_reference_paths(entry, Path(directory))

    def test_legacy_rejects_nul_without_changing_relative_names(self):
        with self.assertRaisesRegex(
            legacy_import.LegacyAuthoringImportError, "non-empty relative path"
        ):
            legacy_import._safe_relative("audio/bad\x00.wav", "audio")
        for value in (" clip.wav ", "C:clip.wav"):
            self.assertEqual(legacy_import._safe_relative(value, "audio"), Path(value))

    def test_comparison_references_reject_nul_with_the_unsafe_path_error(self):
        variant: dict[str, object] = {
            "voice_character": "Voice",
            "voice_speaker": "voice",
            "ordered_references": [{"path": "audio/bad\x00.wav", "sha256": "a" * 64}],
        }
        with self.assertRaisesRegex(
            voice_repair_comparison.VoiceRepairComparisonError, "path is unsafe"
        ):
            voice_repair_comparison._validate_variants([variant])
        variant["ordered_references"] = [
            {"path": " audio/clip.wav ", "sha256": "a" * 64}
        ]
        voice_repair_comparison._validate_variants([variant])

    def test_voice_references_reject_nul_and_keep_path_normalization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            with self.assertRaisesRegex(VoiceManifestError, "safe POSIX-relative"):
                voices._contained_manifest_reference(manifest, "audio/bad\x00.wav")
            self.assertEqual(
                voices._contained_manifest_reference(manifest, " audio//./clip.wav/ "),
                (root / "audio/clip.wav").resolve(),
            )

    def test_replay_rejects_nul_and_keeps_normalized_relative_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "contained relative path"):
                live_replay._contained_regular_file(root, "audio/bad\x00.wav", "audio")
            path = root / "clip.wav"
            path.write_bytes(b"audio")
            self.assertEqual(
                live_replay._contained_regular_file(root, "./clip.wav", "audio"),
                (path.resolve(), "clip.wav"),
            )

    def test_player_candidate_report_rejects_nul_with_its_domain_error(self):
        with TemporaryDirectory() as directory:
            manifest: dict[str, object] = {
                pregeneration_voices.PLAYER_VOICE_CANDIDATES_FIELD: {
                    "schema": pregeneration_voices.PLAYER_VOICE_CANDIDATES_SCHEMA,
                    "schema_version": pregeneration_voices.PLAYER_VOICE_CANDIDATES_VERSION,
                    "story_index_sha256": "a" * 64,
                    "candidate_report": "evidence/bad\x00.json",
                    "candidate_report_sha256": "b" * 64,
                    "variants": [],
                }
            }
            with self.assertRaisesRegex(
                pregeneration_voices.PregenerationVoiceError, "report path is invalid"
            ):
                pregeneration_voices._manifest_candidate_variants(
                    manifest,
                    voices.CharacterVoiceRegistry(),
                    Path(directory) / "manifest.json",
                    "a" * 64,
                )

    def test_cohort_source_rejects_nul_and_keeps_native_path_normalization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                cohort_bundle.CohortReviewError, "path is unsafe"
            ):
                cohort_bundle._contained_source_path(root, "audio/bad\x00.wav", "audio")
            self.assertEqual(
                cohort_bundle._contained_source_path(
                    root, "./audio//clip.wav", "audio"
                ),
                (root / "audio/clip.wav").resolve(),
            )

    def test_render_artifact_rejects_nul_and_keeps_native_path_normalization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                reference_render_comparison.ReferenceRenderComparisonError,
                "artifact leaves its root",
            ):
                reference_render_comparison._contained_file(root, "audio/bad\x00.wav")
            path = root / "clip.wav"
            path.write_bytes(b"audio")
            self.assertEqual(
                reference_render_comparison._contained_file(root, "./clip.wav"),
                path.resolve(),
            )

    def test_offline_authority_rejects_nul_in_a_path_object(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                offline_fallback_authority.OfflineFallbackAuthorityError,
                "authority path is unsafe",
            ):
                offline_fallback_authority._snapshot_authority_at_path(
                    Path("./evidence//bad\x00.json"), directory
                )

    def test_successor_audio_rejects_nul_and_keeps_posix_path_normalization(self):
        queue_id = "queue-1"
        audio_sha256 = "a" * 64
        candidate_id = canonical_document_sha256(
            {
                "queue_id": queue_id,
                "authority": "approved",
                "audio_sha256": audio_sha256,
            }
        )
        candidate_ids = [candidate_id, "b" * 64]
        projection: terminal_conflict_successor._ResolutionProjection = {
            "case_id": canonical_document_sha256(
                {
                    "queue_id": queue_id,
                    "queue_record_sha256": "c" * 64,
                    "text_sha256": "d" * 64,
                    "candidate_ids": candidate_ids,
                }
            ),
            "queue_id": queue_id,
            "line_id": "line-1",
            "queue_record_sha256": "c" * 64,
            "text_sha256": "d" * 64,
            "candidate_ids": candidate_ids,
            "reviewed_at": "2026-10-09T00:00:00+00:00",
            "decision": "selected_candidate",
            "selected_candidate_id": candidate_id,
            "selected_authority": "approved",
            "selected_audio": "audio/bad\x00.wav",
            "selected_audio_sha256": audio_sha256,
            "sample_rate": 24000,
            "sample_count": 24000,
        }
        with self.assertRaisesRegex(
            terminal_conflict_successor.TerminalConflictSuccessorError,
            "Selected successor audio path is invalid",
        ):
            terminal_conflict_successor._validate_resolution_projection(
                projection, queue_id
            )
        projection["selected_audio"] = "./audio//clip.wav"
        self.assertEqual(
            terminal_conflict_successor._validate_resolution_projection(
                projection, queue_id
            )[1],
            terminal_conflict_successor.APPLY_APPROVED_OUTCOME,
        )

    def test_optional_portrait_rejects_nul_with_the_filename_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            staging = root / "staging"
            with self.assertRaisesRegex(
                source_reference_quality.SourceReferenceQualityError,
                "must be a filename",
            ):
                source_reference_quality._copy_optional_portrait(
                    root, "bad\x00.png", "variant", staging, []
                )
            self.assertIsNone(
                source_reference_quality._copy_optional_portrait(
                    root, "./missing", "variant", staging, []
                )
            )
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
