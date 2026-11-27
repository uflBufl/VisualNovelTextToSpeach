import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np

from tests.pregeneration_fixtures import clean_wav_bytes
from tests.voice_manifest_fixtures import write_player_candidate_manifest
from vntts.pregeneration_voices import PLAYER_VOICE_CANDIDATES_FIELD, ReferenceRanking
from vntts.reference_selection import study, typicality_scores, unit_embedding


class ReferenceTypicalityTest(unittest.TestCase):
    def _catalog(self, root, count):
        manifest = write_player_candidate_manifest(
            root,
            "f" * 64,
            quality_scores=(100,) * count,
            admission_policy="audio-only",
        )
        document = json.loads(manifest.read_bytes())
        variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
        for index, variant in enumerate(variants):
            payload = clean_wav_bytes(amplitude=(index + 1) / 10)
            (root / document["voices"][index]["references"][0]).write_bytes(payload)
            variant["reference_sha256"] = hashlib.sha256(payload).hexdigest()
        manifest.write_text(json.dumps(document))
        return manifest, document

    def test_runtime_selector_skips_model_stack_for_small_pools(self):
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            manifest, document = self._catalog(Path(directory), 2)
            with patch(
                "vntts.reference_selection.find_spec",
                side_effect=AssertionError("Sparse selection must stay model-free"),
            ):
                selector = make_reference_selector(manifest, speech_backend="moss-tts")
                selected = selector("Rhiannon")
                checksum = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"][0][
                    "reference_sha256"
                ]
                self.assertEqual(selected, ReferenceRanking(checksum, checksum))
                self.assertEqual(selector("Nobody"), ReferenceRanking(None, None))

    def test_backend_admission_policy_is_preserved(self):
        from vntts.pregeneration_voices import PregenerationVoiceError
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            manifest, document = self._catalog(Path(directory), 2)
            with self.assertRaisesRegex(PregenerationVoiceError, "refreshing"):
                make_reference_selector(manifest, speech_backend="qwen-tts")("Rhiannon")
            document[PLAYER_VOICE_CANDIDATES_FIELD]["reference_admission_policy"] = (
                "transcript-bound"
            )
            manifest.write_text(json.dumps(document))
            report = study(
                manifest,
                "Rhiannon",
                None,
                lambda _: self.fail("Small pool should not load an encoder"),
                speech_backend="qwen-tts",
            )
            self.assertEqual(report["fit_count"], 2)
            self.assertIsNotNone(report["baseline"])
            selection = make_reference_selector(manifest, speech_backend="qwen-tts")(
                "Rhiannon"
            )
            self.assertEqual(selection.preferred, report["baseline"]["sha256"])
            self.assertEqual(selection.typical, selection.preferred)
            with self.assertRaisesRegex(PregenerationVoiceError, "refreshing"):
                make_reference_selector(manifest, speech_backend="moss-tts")("Rhiannon")

    def test_runtime_selector_missing_stack_is_actionable(self):
        from vntts.reference_selection import (
            ReferenceSelectionUnavailable,
            make_reference_selector,
        )

        with TemporaryDirectory() as directory:
            manifest, _ = self._catalog(Path(directory), 3)
            with patch("vntts.reference_selection.find_spec", return_value=None):
                with self.assertRaisesRegex(
                    ReferenceSelectionUnavailable, "Repair the application installation"
                ):
                    make_reference_selector(manifest, speech_backend="moss-tts")(
                        "Rhiannon"
                    )

    def test_runtime_selector_reuses_model_and_rejects_changed_sources(self):
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, document = self._catalog(root, 3)
            encoder = Mock(return_value=(1, 0))
            cancelled = Mock()
            with (
                patch("vntts.reference_selection.find_spec", return_value=object()),
                patch("vntts.reference_selection.version", return_value="1.1.1"),
                patch(
                    "vntts.reference_selection.managed_speaker_identity_installation",
                    return_value=root,
                ),
                patch(
                    "vntts.reference_selection.resolve_managed_speaker_identity_model",
                    return_value=root,
                ) as resolver,
                patch(
                    "vntts.reference_selection.make_speechbrain_embedder",
                    return_value=encoder,
                ) as factory,
                patch(
                    "vntts.reference_selection.delivery_features",
                    return_value=None,
                ),
            ):
                digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
                selector = make_reference_selector(
                    manifest,
                    speech_backend="moss-tts",
                    expected_manifest_sha256=digest,
                    check_cancelled=cancelled,
                )
                selection = selector("Rhiannon")
                self.assertEqual(selection.preferred, selection.typical)
                self.assertEqual(selector("rhiannon"), selection)
                self.assertEqual(encoder.call_count, 3)
                factory.assert_called_once_with(root)
                resolver.assert_called_once_with()
                self.assertGreaterEqual(cancelled.call_count, 9)
                source = root / document["voices"][0]["references"][0]
                original = source.read_bytes()
                source.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "Reference changed"):
                    selector("Rhiannon")
                source.write_bytes(original)
                manifest.write_text(manifest.read_text() + " ")
                with self.assertRaisesRegex(ValueError, "manifest changed"):
                    selector("Rhiannon")

    def test_clean_install_prepares_pinned_model_on_first_use(self):
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = self._catalog(root, 3)
            progress = Mock()
            with (
                patch("vntts.reference_selection.find_spec", return_value=object()),
                patch("vntts.reference_selection.version", return_value="1.1.1"),
                patch(
                    "vntts.reference_selection.managed_speaker_identity_installation",
                    return_value=root / "not-installed",
                ),
                patch(
                    "vntts.reference_selection.install_managed_speaker_identity_model"
                ) as install,
                patch(
                    "vntts.reference_selection.resolve_managed_speaker_identity_model",
                    return_value=root,
                ),
                patch(
                    "vntts.reference_selection.make_speechbrain_embedder",
                    return_value=Mock(return_value=(1, 0)),
                ),
                patch("vntts.reference_selection.delivery_features", return_value=None),
            ):
                selector = make_reference_selector(
                    manifest, speech_backend="moss-tts", progress=progress
                )
                self.assertIsNotNone(selector("Rhiannon").preferred)
                selector("Rhiannon")
            install.assert_called_once_with()
            progress.assert_called_once()
            self.assertIn("89 MB", progress.call_args.args[0])

    def test_offline_preparation_fails_without_using_old_selection(self):
        from vntts.authoring.speaker_identity_model import SpeakerIdentityModelError
        from vntts.reference_selection import (
            ReferenceSelectionUnavailable,
            make_reference_selector,
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = self._catalog(root, 3)
            with (
                patch("vntts.reference_selection.find_spec", return_value=object()),
                patch("vntts.reference_selection.version", return_value="1.1.1"),
                patch(
                    "vntts.reference_selection.managed_speaker_identity_installation",
                    return_value=root / "not-installed",
                ),
                patch(
                    "vntts.reference_selection.install_managed_speaker_identity_model",
                    side_effect=SpeakerIdentityModelError("offline"),
                ),
                patch("vntts.reference_selection.make_speechbrain_embedder") as encoder,
            ):
                with self.assertRaisesRegex(
                    ReferenceSelectionUnavailable, "then retry"
                ):
                    make_reference_selector(manifest, speech_backend="moss-tts")(
                        "Rhiannon"
                    )
            encoder.assert_not_called()

    def test_sparse_selection_carries_rejected_checksums_through_cache(self):
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, document = self._catalog(root, 2)
            variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
            bad = clean_wav_bytes(amplitude=0)
            (root / document["voices"][0]["references"][0]).write_bytes(bad)
            variants[0]["reference_sha256"] = hashlib.sha256(bad).hexdigest()
            manifest.write_text(json.dumps(document))
            selector = make_reference_selector(manifest, speech_backend="moss-tts")
            expected = ReferenceRanking(
                variants[1]["reference_sha256"],
                variants[1]["reference_sha256"],
                frozenset({variants[0]["reference_sha256"]}),
            )
            self.assertEqual(selector("Rhiannon"), expected)
            self.assertEqual(selector("rhiannon"), expected)

    def test_runtime_selector_keeps_model_integrity_errors(self):
        from vntts.authoring.speaker_identity_model import SpeakerIdentityModelError
        from vntts.reference_selection import make_reference_selector

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = self._catalog(root, 3)
            with (
                patch("vntts.reference_selection.find_spec", return_value=object()),
                patch("vntts.reference_selection.version", return_value="1.1.1"),
                patch(
                    "vntts.reference_selection.managed_speaker_identity_installation",
                    return_value=root,
                ),
                patch(
                    "vntts.reference_selection.resolve_managed_speaker_identity_model",
                    side_effect=SpeakerIdentityModelError("checksum mismatch"),
                ),
            ):
                with self.assertRaisesRegex(SpeakerIdentityModelError, "checksum"):
                    make_reference_selector(manifest, speech_backend="moss-tts")(
                        "Rhiannon"
                    )

    def test_held_out_bank_scores_but_never_selects_the_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_player_candidate_manifest(
                root,
                "f" * 64,
                quality_scores=(100, 100, 100, 100, 90, 100),
                admission_policy="audio-only",
            )
            document = json.loads(manifest.read_bytes())
            variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
            vectors = {}
            for index, variant in enumerate(variants):
                # The first four-second clip is an outlier. The holdout points
                # directly at it and would favor it if allowed to vote.
                payload = clean_wav_bytes(
                    amplitude=0 if index == 5 else (index + 1) / 10
                )
                (root / document["voices"][index]["references"][0]).write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                variant["reference_sha256"] = digest
                vectors[payload] = [
                    (1, 0),
                    (0, 1),
                    (-0.3, 1),
                    (1, 0),
                    (0.2, 1),
                    (0, 0),
                ][index]
            variants[3]["source_bank"] = "reserve.bnk"
            manifest.write_text(json.dumps(document))
            preview = root / "preview.wav"
            preview.write_bytes(clean_wav_bytes(amplitude=0.7))
            vectors[preview.read_bytes()] = (-1, 0)
            result = study(
                manifest, "Rhiannon", "reserve.bnk", vectors.__getitem__, [preview]
            )
            self.assertEqual(
                result["baseline"]["sha256"], variants[0]["reference_sha256"]
            )
            self.assertEqual(
                result["representative"]["sha256"], variants[1]["reference_sha256"]
            )
            self.assertEqual(result["fit_count"], 4)
            self.assertEqual(result["holdout_count"], 1)
            self.assertEqual(result["baseline"]["mean_holdout_cosine"], 1)
            self.assertEqual(result["representative"]["mean_holdout_cosine"], 0)
            self.assertEqual(result["renders"][0]["mean_holdout_cosine"], -1)
            self.assertEqual(
                result["excluded_preflight"][0]["sha256"],
                variants[5]["reference_sha256"],
            )
            self.assertEqual(result["originals_by_typicality"][0]["quality_score"], 90)
            with self.assertRaisesRegex(ValueError, "No passing originals"):
                study(manifest, "Rhiannon", "absent.bnk", vectors.__getitem__)
            with self.assertRaisesRegex(ValueError, "independent originals"):
                study(manifest, "Rhiannon", None, vectors.__getitem__, [preview])
            full = study(manifest, "Rhiannon", None, vectors.__getitem__)
            self.assertEqual(full["fit_count"], 5)
            self.assertIsNone(full["representative"]["mean_holdout_cosine"])

    def test_mean_excludes_self_and_preserves_exact_ties(self):
        vectors = [np.array(v) for v in [(1.0, 0.0), (1.0, 0.0), (0.0, 1.0)]]
        np.testing.assert_allclose(typicality_scores(vectors), [0.5, 0.5, 0])
        with self.assertRaisesRegex(ValueError, "three originals"):
            typicality_scores(vectors[:2])
        for invalid in ([], [0, 0], [float("nan")]):
            with self.assertRaisesRegex(ValueError, "invalid embedding"):
                unit_embedding(lambda _: invalid, b"")

    def test_small_catalog_keeps_safe_baseline_without_optional_runtimes(self):
        from vntts.reference_selection import (
            catalog_characters,
            select_reference,
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_player_candidate_manifest(
                root, "f" * 64, quality_scores=(100, 100), admission_policy="audio-only"
            )
            document = json.loads(manifest.read_bytes())
            variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
            for index, variant in enumerate(variants):
                payload = clean_wav_bytes(amplitude=(index + 1) / 10)
                (root / document["voices"][index]["references"][0]).write_bytes(payload)
                variant["reference_sha256"] = hashlib.sha256(payload).hexdigest()
            manifest.write_text(json.dumps(document))

            def unavailable(_):
                self.fail("Small catalogs must not load speaker or pitch runtimes")

            report = select_reference(
                manifest, "Rhiannon", unavailable, feature_extractor=unavailable
            )
            self.assertEqual(report["selection_method"], "baseline")
            self.assertEqual(
                report["selected"]["sha256"], variants[0]["reference_sha256"]
            )
            self.assertIsNone(report["selected"]["mean_peer_cosine"])
            self.assertEqual(
                catalog_characters(manifest),
                [{"character": "Rhiannon", "eligible_catalog_clips": 2}],
            )
            absent = select_reference(
                manifest, "Nobody", unavailable, feature_extractor=unavailable
            )
            self.assertIsNone(absent["selected"])
            self.assertEqual(absent["selection_method"], "abstain")

    def test_unusable_embeddings_are_skipped_but_runtime_failures_surface(self):
        from vntts.reference_selection import select_reference

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_player_candidate_manifest(
                root,
                "f" * 64,
                quality_scores=(100, 100, 100),
                admission_policy="audio-only",
            )
            document = json.loads(manifest.read_bytes())
            variants = document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"]
            vectors = {}
            for index, variant in enumerate(variants):
                payload = clean_wav_bytes(amplitude=(index + 1) / 10)
                (root / document["voices"][index]["references"][0]).write_bytes(payload)
                variant["reference_sha256"] = hashlib.sha256(payload).hexdigest()
                vectors[payload] = [(0, 0), (1, 0), (0, 1)][index]
            manifest.write_text(json.dumps(document))

            def no_features(_):
                self.fail("Insufficient embeddings must not load the pitch runtime")

            report = select_reference(
                manifest, "Rhiannon", vectors.__getitem__, feature_extractor=no_features
            )
            self.assertEqual(report["selection_method"], "baseline")
            self.assertEqual(report["fit_count"], 2)
            self.assertEqual(
                report["selected"]["sha256"], variants[1]["reference_sha256"]
            )
            self.assertIn(
                "invalid embedding", report["excluded_preflight"][0]["reasons"][0]
            )

            def unavailable(_):
                raise RuntimeError("Model unavailable")

            with self.assertRaisesRegex(RuntimeError, "Model unavailable"):
                select_reference(manifest, "Rhiannon", unavailable)

            for index, variant in enumerate(variants):
                payload = clean_wav_bytes(amplitude=0)
                (root / document["voices"][index]["references"][0]).write_bytes(payload)
                variant["reference_sha256"] = hashlib.sha256(payload).hexdigest()
            manifest.write_text(json.dumps(document))
            empty = select_reference(manifest, "Rhiannon", unavailable)
            self.assertEqual(empty["selection_method"], "abstain")
            self.assertIsNone(empty["selected"])

    def test_frozen_delivery_criteria_and_typical_fallback(self):
        from unittest.mock import patch

        from vntts.reference_selection import select_reference

        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text("{}")
            rows = []
            features = {}
            for index in range(8):
                payload = str(index).encode()
                path = root / f"{index}.wav"
                path.write_bytes(payload)
                rows.append(
                    {
                        "path": path.name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "quality_score": 100,
                        "text": "A calm statement.",
                        "mean_peer_cosine": 0.95 - index / 100,
                    }
                )
                features[payload] = {
                    "pitch_hz": [1, 4, 2, 3, 5, 6, 7, 8][index],
                    "pitch_span_st": [1, 2, 3, 4, 5, 6, 7, 8][index],
                    "level_span_db": [1, 2, 3, 4, 5, 6, 7, 8][index],
                }
            study_report = {
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "character": "Rhiannon",
                "fit_count": len(rows),
                "representative": rows[0],
                "baseline": rows[-1],
                "originals_by_typicality": rows,
            }
            with patch("vntts.reference_selection.study", return_value=study_report):
                report = select_reference(
                    manifest, "Rhiannon", None, feature_extractor=features.__getitem__
                )
                self.assertEqual(report["selection_method"], "steady")
                self.assertEqual(report["selected"]["sha256"], rows[1]["sha256"])
                self.assertEqual(report["selected"]["mean_peer_cosine_rank"], 0.8125)
                for text in (
                    "A question?",
                    "Loud!",
                    "A pause...",
                    "A pause…",
                    "*whispers*",
                ):
                    rows[1]["text"] = text
                    fallback = select_reference(
                        manifest,
                        "Rhiannon",
                        None,
                        feature_extractor=features.__getitem__,
                    )
                    self.assertEqual(fallback["selection_method"], "typical")
                    self.assertEqual(fallback["selected"], rows[0])
                rows[1]["text"] = "A calm statement."
                # Tied cosine percentiles use average ranks; none is top-quartile.
                for row in rows:
                    row["mean_peer_cosine"] = 0.9
                tied = select_reference(
                    manifest, "Rhiannon", None, feature_extractor=features.__getitem__
                )
                self.assertEqual(tied["selection_method"], "typical")
                unusable = select_reference(
                    manifest, "Rhiannon", None, feature_extractor=lambda _: None
                )
                self.assertEqual(unusable["selection_method"], "typical")
                self.assertEqual(unusable["steady_pool_count"], 0)
                self.assertEqual(len(unusable["excluded_features"]), len(rows))
