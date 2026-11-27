import hashlib
import io
import json
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from scripts.study_reference_transcript_sensitivity import (
    acoustic_candidates,
    main,
    replay_transcript_sensitivity,
    transcribe_sources,
    transcript_discrepancy,
)
from tests.voice_manifest_fixtures import write_player_candidate_manifest
from vntts.pregeneration_voices import PLAYER_VOICE_CANDIDATES_FIELD


class TranscriptSensitivityTest(unittest.TestCase):
    def make_study(
        self, root: Path
    ) -> tuple[Path, dict[str, Any], Callable[[bytes], dict[str, float]]]:
        manifest = write_player_candidate_manifest(
            root, "f" * 64, quality_scores=(100,) * 16, admission_policy="audio-only"
        )
        document = json.loads(manifest.read_bytes())
        variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
        rows: list[dict[str, Any]] = []
        features: dict[bytes, dict[str, float]] = {}
        for index, variant in enumerate(variants):
            variant["variant_id"] = f"{index + 1:064x}"
            path = root / document["voices"][index]["references"][0]
            payload = path.read_bytes()
            text = "A calm statement." if index != 3 else "A question?"
            variant["source_excerpts"][0]["text"] = text
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "quality_score": 100,
                    "text": text,
                    "source_bank": variant["source_bank"],
                    "source_line_ids": variant["source_line_ids"],
                    "mean_peer_cosine": 0.95 - index / 100,
                }
            )
            features[payload] = {
                "pitch_hz": [1, 2, 6, 7, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 16][
                    index
                ],
                "pitch_span_st": index,
                "level_span_db": index,
            }
        manifest.write_text(json.dumps(document))
        report = {
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "character": "Rhiannon",
            "fit_count": len(rows),
            "representative": rows[0],
            "originals_by_typicality": rows,
        }
        return manifest, report, features.__getitem__

    def test_historical_missing_text_uses_verified_catalog_and_normalized_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            manifest, report, features = self.make_study(Path(directory))
            expected_fallback = dict(report["representative"])
            for omit_originals, omit_fallback in (
                (True, True),
                (False, True),
                (True, False),
            ):
                with self.subTest(originals=omit_originals, fallback=omit_fallback):
                    historical = json.loads(json.dumps(report))
                    if omit_originals:
                        for row in historical["originals_by_typicality"]:
                            del row["text"]
                    if omit_fallback:
                        del historical["representative"]["text"]
                    snapshot = json.dumps(historical)
                    result = replay_transcript_sensitivity(
                        manifest,
                        historical,
                        {"transcripts": []},
                        feature_extractor=features,
                    )
                    self.assertEqual(result["representative"], expected_fallback)
                    self.assertEqual(
                        result["decisions"]["asr"]["selected"], expected_fallback
                    )
                    self.assertEqual(
                        [row["text"] for row in result["acoustic_candidates"]],
                        ["A calm statement.", "A question?"],
                    )
                    self.assertEqual(json.dumps(historical), snapshot)
            historical["representative"]["text"] = "Forged fallback text."
            with self.assertRaisesRegex(ValueError, "fallback must be"):
                acoustic_candidates(manifest, historical, feature_extractor=features)

    def test_replays_all_acoustic_candidates_and_preserves_frozen_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            manifest, report, features = self.make_study(Path(directory))
            candidates = acoustic_candidates(
                manifest, report, feature_extractor=features
            )
            rows = report["originals_by_typicality"]
            self.assertEqual(
                [row["sha256"] for row in candidates["acoustic_candidates"]],
                [rows[2]["sha256"], rows[3]["sha256"]],
            )
            cache: dict[str, Any] = {
                "model_revision": "fixed-revision",
                "transcripts": [
                    {"sha256": rows[2]["sha256"], "text": "A calm statement?"},
                    {
                        "sha256": rows[3]["sha256"],
                        "text": "Another entirely different statement.",
                    },
                ],
            }
            result = replay_transcript_sensitivity(
                manifest, report, cache, feature_extractor=features
            )
            decisions = result["decisions"]
            self.assertEqual(
                decisions["catalog"]["selected"]["sha256"], rows[2]["sha256"]
            )
            self.assertEqual(decisions["asr"]["selected"]["sha256"], rows[3]["sha256"])
            self.assertEqual(decisions["sensitivity"]["selected"], rows[0])
            self.assertEqual(decisions["sensitivity"]["selection_method"], "typical")
            self.assertEqual(
                result["transcript_provenance"], {"model_revision": "fixed-revision"}
            )
            cache["transcripts"][1]["text"] = "A question."
            lexical_match = replay_transcript_sensitivity(
                manifest, report, cache, feature_extractor=features
            )
            self.assertEqual(
                lexical_match["decisions"]["sensitivity"]["selected"]["sha256"],
                rows[3]["sha256"],
            )
            cache["transcripts"].pop(0)
            missing = replay_transcript_sensitivity(
                manifest, report, cache, feature_extractor=features
            )
            self.assertEqual(missing["unknown_transcript_sha256s"], [rows[2]["sha256"]])
            for arm in ("asr", "sensitivity"):
                self.assertEqual(missing["decisions"][arm]["selected"], rows[0])
                self.assertEqual(
                    missing["decisions"][arm]["selection_reason"],
                    "transcript_coverage_unknown",
                )

    def test_discrepancy_rule_has_frozen_boundaries_and_skips_single_names(
        self,
    ) -> None:
        for expected, observed, status in (
            (
                "These are yours, Vertin.",
                "These are yours burton!",
                "minor_or_uncertain",
            ),
            ("these are yours vertin", "these are yours burton", "minor_or_uncertain"),
            ("We should go!", "we should go.", "lexical_match"),
            ("one two three four five", "four five", "material"),
            ("one two three four five", "three four five", "minor_or_uncertain"),
            ("a b c d e f g h i j k l", "d e f g h i j k l", "material"),
            ("a b c d e f g h i j k l m", "d e f g h i j k l m", "minor_or_uncertain"),
            ("A statement", "", "unknown"),
            ("A statement", None, "unknown"),
        ):
            with self.subTest(expected=expected, observed=observed):
                self.assertEqual(
                    transcript_discrepancy(expected, observed)["status"], status
                )

    def test_rejects_stale_sources_manifests_and_duplicate_cache_entries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, report, features = self.make_study(root)
            row = report["originals_by_typicality"][2]
            entry = {"sha256": row["sha256"], "text": row["text"]}
            with self.assertRaisesRegex(ValueError, "Duplicate transcript"):
                replay_transcript_sensitivity(
                    manifest,
                    report,
                    {"transcripts": [entry, entry]},
                    feature_extractor=features,
                )
            with self.assertRaisesRegex(ValueError, "every fitted original"):
                acoustic_candidates(
                    manifest, {**report, "fit_count": 20}, feature_extractor=features
                )
            # Source validation includes originals outside the acoustic shortlist.
            (root / report["originals_by_typicality"][-1]["path"]).write_bytes(
                b"changed"
            )
            with self.assertRaisesRegex(ValueError, "not bound"):
                acoustic_candidates(manifest, report, feature_extractor=features)
            manifest.write_text('{"changed": true}')
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                acoustic_candidates(manifest, report, feature_extractor=features)

    def test_rejects_unbound_paths_and_forged_metadata_before_feature_extraction(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            manifest, report, _ = self.make_study(Path(directory))
            row = report["originals_by_typicality"][2]

            def unexpected(_payload: bytes) -> None:
                self.fail("Invalid study rows must not reach feature extraction")

            for bad_path in ("../outside.wav", "/tmp/outside.wav"):
                original = row["path"]
                row["path"] = bad_path
                with self.assertRaisesRegex(ValueError, "relative"):
                    acoustic_candidates(manifest, report, feature_extractor=unexpected)
                row["path"] = original
            row["text"] = "Forged text."
            with self.assertRaisesRegex(ValueError, "metadata differs"):
                acoustic_candidates(manifest, report, feature_extractor=unexpected)

    def test_cli_records_checksums_and_small_catalog_keeps_baseline(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, report, _ = self.make_study(root)
            report["originals_by_typicality"] = report["originals_by_typicality"][:2]
            report["fit_count"] = 2
            report_path, cache_path = root / "study.json", root / "cache.json"
            report_path.write_text(json.dumps(report))
            cache_path.write_text('{"transcripts": []}')
            with (
                patch(
                    "sys.argv",
                    ["replay", str(manifest), str(report_path), str(cache_path)],
                ),
                patch(
                    "scripts.study_reference_transcript_sensitivity.print_json"
                ) as output,
            ):
                self.assertEqual(main(), 0)
            result = output.call_args.args[0]
            self.assertEqual(
                result["study_report_sha256"],
                hashlib.sha256(report_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result["transcript_cache_sha256"],
                hashlib.sha256(cache_path.read_bytes()).hexdigest(),
            )
            for decision in result["decisions"].values():
                self.assertEqual(decision["selection_method"], "baseline")
                self.assertEqual(decision["selected"], report["representative"])

    def test_source_transcription_converts_stereo_and_keeps_source_hashes(self) -> None:
        import numpy as np
        import soundfile as sf

        with TemporaryDirectory() as directory:
            manifest, report, features = self.make_study(Path(directory))
            rows = report["originals_by_typicality"]
            seen: list[str] = []

            def transcribe(payload: bytes) -> str:
                audio, rate = sf.read(io.BytesIO(payload), always_2d=True)
                self.assertEqual(audio.shape[1], 1)
                self.assertEqual(rate, 16000)
                self.assertEqual(sf.info(io.BytesIO(payload)).subtype, "PCM_16")
                if seen:
                    np.testing.assert_allclose(audio, 0.5)
                seen.append(hashlib.sha256(payload).hexdigest())
                return "A question."

            stereo = io.BytesIO()
            sf.write(
                stereo,
                np.array([[0.25, 0.75]] * 1600),
                16000,
                subtype="PCM_16",
                format="WAV",
            )
            original = (manifest.parent / rows[3]["path"]).read_bytes()
            payload = stereo.getvalue()
            (manifest.parent / rows[3]["path"]).write_bytes(payload)
            rows[3]["sha256"] = hashlib.sha256(payload).hexdigest()
            document = json.loads(manifest.read_bytes())
            document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"][3][
                "reference_sha256"
            ] = rows[3]["sha256"]
            manifest.write_text(json.dumps(document))
            report["manifest_sha256"] = hashlib.sha256(
                manifest.read_bytes()
            ).hexdigest()
            cache = transcribe_sources(
                manifest,
                report,
                transcriber=transcribe,
                feature_extractor=lambda raw: features(
                    original if raw == payload else raw
                ),
            )
            self.assertEqual(
                [row["sha256"] for row in cache["transcripts"]],
                [rows[2]["sha256"], rows[3]["sha256"]],
            )
            self.assertEqual(
                [row["asr_input_sha256"] for row in cache["transcripts"]], seen
            )
            self.assertEqual(cache["analyzer"]["repository"], "openai/whisper-tiny.en")


if __name__ == "__main__":
    unittest.main()
