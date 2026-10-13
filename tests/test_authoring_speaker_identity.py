import hashlib
import json
import math
import tomllib
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import DEFAULT, Mock, patch

import numpy as np

from tests.symlink_support import symlink_or_skip
from vntts.authoring import (
    cli_speaker_identity,
    managed_model_installation,
    speaker_identity,
)
from vntts.authoring.cli import create_parser
from vntts.authoring.speaker_identity import (
    SpeakerIdentityError,
    build_labelled_pairs,
    build_reference_inventory,
    build_speaker_identity_report,
    load_labelled_pairs,
    load_reference_inventory,
    make_speechbrain_embedder,
    require_speechbrain_runtime,
)
from vntts.authoring.speaker_identity_model import (
    MODEL_FILES,
    SpeakerIdentityModelError,
    install_managed_speaker_identity_model,
    managed_speaker_identity_status,
    resolve_managed_speaker_identity_model,
)


def _wav(path, value):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(int(value).to_bytes(2, "little", signed=True) * 1600)


def _fixture(root):
    references = root / "references"
    references.mkdir()
    entries = []
    for index, character in enumerate(
        ("Adult", "Adult", "Other", "Adult", "Adult", "Child")
    ):
        path = references / f"reference-{index}.wav"
        _wav(path, index + 1)
        entries.append(
            {
                "character": f"{character} {index}",
                "speaker": f"speaker-{index}",
                "references": [f"references/{path.name}"],
            }
        )
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"version": 2, "voices": entries}), "utf-8")
    return manifest


class SpeakerIdentityTest(unittest.TestCase):
    def test_cli_reports_stay_outside_managed_model_for_online_and_offline_routes(self):
        parser = create_parser()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            model = root / "model"
            model.mkdir()
            alias = root / "model-alias"
            external = root / "report.json"
            for offline in (False, True):
                for output in (
                    model / "report.json",
                    model / "sub" / "report.json",
                    alias / "report.json",
                    external,
                ):
                    with self.subTest(offline=offline, output=output):
                        if output.parent == alias and not alias.is_symlink():
                            symlink_or_skip(alias, model, target_is_directory=True)
                        arguments = parser.parse_args(
                            [
                                "speaker-identity-evaluate",
                                str(root / "inventory.json"),
                                str(root / "labels.json"),
                                "--output",
                                str(output),
                                *(["--offline"] if offline else []),
                            ]
                        )
                        with patch.multiple(
                            cli_speaker_identity,
                            load_reference_inventory=DEFAULT,
                            load_labelled_pairs=DEFAULT,
                            require_speechbrain_runtime=DEFAULT,
                            resolve_managed_speaker_identity_model=DEFAULT,
                            install_managed_speaker_identity_model=DEFAULT,
                            make_speechbrain_embedder=DEFAULT,
                            installed_model_descriptor=DEFAULT,
                            build_speaker_identity_report=DEFAULT,
                            write_speaker_identity_report=DEFAULT,
                            print_json=DEFAULT,
                        ) as operations:
                            operations[
                                "resolve_managed_speaker_identity_model"
                            ].return_value = model
                            operations[
                                "install_managed_speaker_identity_model"
                            ].return_value = {"model_directory": str(model)}
                            report = {"report_id": "report-id", "pair_count": 2}
                            operations[
                                "build_speaker_identity_report"
                            ].return_value = report
                            if output == external:
                                self.assertEqual(
                                    cli_speaker_identity.handle(arguments), 0
                                )
                                operations[
                                    "make_speechbrain_embedder"
                                ].assert_called_once_with(model, device="cpu")
                                operations[
                                    "write_speaker_identity_report"
                                ].assert_called_once_with(report, output)
                                operations["print_json"].assert_called_once_with(
                                    {"output": str(output), **report}
                                )
                            else:
                                with self.assertRaisesRegex(
                                    SpeakerIdentityError,
                                    "outside the immutable model",
                                ):
                                    cli_speaker_identity.handle(arguments)
                                for operation in (
                                    "make_speechbrain_embedder",
                                    "build_speaker_identity_report",
                                    "write_speaker_identity_report",
                                ):
                                    operations[operation].assert_not_called()
                            selected = (
                                "resolve_managed_speaker_identity_model"
                                if offline
                                else "install_managed_speaker_identity_model"
                            )
                            operations[selected].assert_called_once_with()
            self.assertEqual(list(model.iterdir()), [])
            self.assertFalse(external.exists())

    def test_inventory_labels_and_held_out_threshold_are_checksum_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            inventory = build_reference_inventory(manifest)
            ids = [item["reference_id"] for item in inventory["references"]]
            pairs = [
                {
                    "left_reference_id": ids[0],
                    "right_reference_id": ids[1],
                    "partition": "fit",
                    "relationship": "same-speaker",
                },
                {
                    "left_reference_id": ids[0],
                    "right_reference_id": ids[2],
                    "partition": "fit",
                    "relationship": "different-speaker",
                },
                {
                    "left_reference_id": ids[3],
                    "right_reference_id": ids[4],
                    "partition": "held-out",
                    "relationship": "same-speaker",
                },
                {
                    "left_reference_id": ids[3],
                    "right_reference_id": ids[5],
                    "partition": "held-out",
                    "relationship": "same-character/different-age",
                },
            ]
            labels = build_labelled_pairs(inventory, pairs)
            vectors = {
                item["sha256"]: vector
                for item, vector in zip(
                    inventory["references"],
                    (
                        (1.0, 0.0),
                        (0.99, 0.1),
                        (0.0, 1.0),
                        (0.98, 0.2),
                        (0.97, 0.24),
                        (0.2, 0.98),
                    ),
                    strict=True,
                )
            }

            with patch.object(
                speaker_identity, "_sha256_bytes", wraps=speaker_identity._sha256_bytes
            ) as rehash_reference:
                report = build_speaker_identity_report(
                    inventory,
                    labels,
                    lambda payload: np.asarray(
                        vectors[hashlib.sha256(payload).hexdigest()]
                    ),
                    {"model_id": "injected-test-model"},
                )
            rehash_reference.assert_not_called()

            self.assertTrue(report["fit"]["separated"])
            self.assertTrue(report["threshold_eligible"])
            self.assertEqual(report["held_out"]["boundary_violation_count"], 0)
            self.assertEqual(report["held_out"]["confusion"]["true_positive"], 1)
            self.assertEqual(report["held_out"]["confusion"]["true_negative"], 1)

            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), "utf-8")
            _wav(root / inventory["references"][0]["path"], 100)
            with self.assertRaisesRegex(SpeakerIdentityError, "no longer matches"):
                load_reference_inventory(inventory_path)

    def test_duplicate_pair_cannot_leak_between_fit_and_held_out(self):
        with TemporaryDirectory() as directory:
            inventory = build_reference_inventory(_fixture(Path(directory)))
            left, right = [item["reference_id"] for item in inventory["references"][:2]]
            pair = {
                "left_reference_id": left,
                "right_reference_id": right,
                "partition": "fit",
                "relationship": "same-speaker",
            }
            leaked = {**pair, "partition": "held-out"}
            with self.assertRaisesRegex(SpeakerIdentityError, "leaks"):
                build_labelled_pairs(inventory, [pair, leaked])

    def test_inventory_rejects_duplicate_reference_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            document = json.loads(manifest.read_text("utf-8"))
            document["voices"][0]["references"] *= 2
            manifest.write_text(json.dumps(document), "utf-8")

            with self.assertRaisesRegex(SpeakerIdentityError, "repeats"):
                build_reference_inventory(manifest)

    def test_identical_audio_cannot_cross_fit_and_held_out(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            duplicate = root / "references/reference-duplicate.wav"
            duplicate.write_bytes((root / "references/reference-0.wav").read_bytes())
            document = json.loads(manifest.read_text("utf-8"))
            document["voices"].append(
                {
                    "character": "Duplicate audio",
                    "speaker": "speaker-duplicate",
                    "references": ["references/reference-duplicate.wav"],
                }
            )
            manifest.write_text(json.dumps(document), "utf-8")
            inventory = build_reference_inventory(manifest)
            by_path = {item["path"]: item for item in inventory["references"]}
            fit = {
                "left_reference_id": by_path["references/reference-0.wav"][
                    "reference_id"
                ],
                "right_reference_id": by_path["references/reference-1.wav"][
                    "reference_id"
                ],
                "partition": "fit",
                "relationship": "same-speaker",
            }
            held_out = {
                "left_reference_id": by_path["references/reference-duplicate.wav"][
                    "reference_id"
                ],
                "right_reference_id": by_path["references/reference-3.wav"][
                    "reference_id"
                ],
                "partition": "held-out",
                "relationship": "different-speaker",
            }

            with self.assertRaisesRegex(SpeakerIdentityError, "leaks"):
                build_labelled_pairs(inventory, [fit, held_out])

    def test_manifest_capture_failures_use_the_domain_error(self):
        for removed_during_inspection in (False, True):
            with (
                self.subTest(late=removed_during_inspection),
                TemporaryDirectory() as directory,
            ):
                manifest = _fixture(Path(directory))
                original_read = speaker_identity.read_voice_reference_bytes

                def inspect(voice, reference):
                    payload = original_read(voice, reference)
                    manifest.unlink(missing_ok=True)
                    return payload

                if not removed_during_inspection:
                    manifest.unlink()
                with (
                    patch.object(
                        speaker_identity,
                        "read_voice_reference_bytes",
                        side_effect=inspect,
                    ),
                    self.assertRaises(SpeakerIdentityError),
                ):
                    build_reference_inventory(manifest)

    def test_report_rejects_rehashed_invalid_labels_before_embedding(self):
        with TemporaryDirectory() as directory:
            inventory = build_reference_inventory(_fixture(Path(directory)))
            ids = [item["reference_id"] for item in inventory["references"]]
            labels = build_labelled_pairs(
                inventory,
                [
                    {
                        "left_reference_id": ids[0],
                        "right_reference_id": ids[1],
                        "partition": "fit",
                        "relationship": "same-speaker",
                    },
                    {
                        "left_reference_id": ids[2],
                        "right_reference_id": ids[3],
                        "partition": "held-out",
                        "relationship": "different-speaker",
                    },
                ],
            )
            for invalid_left, reason in (
                (ids[0], "leaks"),
                ("unknown-reference", "unknown reference"),
            ):
                with self.subTest(reference=invalid_left):
                    invalid = {
                        **labels,
                        "pairs": [
                            labels["pairs"][0],
                            {**labels["pairs"][1], "left_reference_id": invalid_left},
                        ],
                    }
                    invalid["labels_id"] = speaker_identity._document_sha256(
                        {
                            key: value
                            for key, value in invalid.items()
                            if key != "labels_id"
                        }
                    )
                    embed = Mock(return_value=(1.0, 0.0))
                    with self.assertRaisesRegex(SpeakerIdentityError, reason):
                        build_speaker_identity_report(inventory, invalid, embed, {})
                    embed.assert_not_called()

    def test_report_rejects_changed_reference_before_embedding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = build_reference_inventory(_fixture(root))
            references = sorted(
                inventory["references"], key=lambda item: item["reference_id"]
            )[:2]
            labels = build_labelled_pairs(
                inventory,
                [
                    {
                        "left_reference_id": references[0]["reference_id"],
                        "right_reference_id": references[1]["reference_id"],
                        "partition": "fit",
                        "relationship": "same-speaker",
                    }
                ],
            )
            _wav(root / references[0]["path"], 100)
            embed = Mock(return_value=(1.0, 0.0))
            with self.assertRaisesRegex(SpeakerIdentityError, "Reference changed"):
                build_speaker_identity_report(inventory, labels, embed, {})
            embed.assert_not_called()

    def test_fit_threshold_preserves_empty_and_boundary_cases(self):
        for positives, negatives, expected_threshold in (
            ((), (), None),
            ((0.2,), (), None),
            ((), (0.8,), None),
            ((0.4,), (0.4,), None),
            ((0.2, 0.8), (0.6, 1.0), None),
            ((0.1, 0.25), (0.75, 1.0), 0.5),
            ((math.nextafter(1.0, 0.0),), (1.0,), math.nextafter(1.0, 0.0)),
            ((1.0,), (math.nextafter(1.0, 2.0),), 1.0),
            ((0.0,), (math.nextafter(0.0, 1.0),), 0.0),
        ):
            with self.subTest(positives=positives, negatives=negatives):
                results = [
                    {
                        "left_reference_id": "left",
                        "right_reference_id": "right",
                        "partition": partition,
                        "relationship": relationship,
                        "cosine_distance": distance,
                    }
                    for partition, relationship, distances in (
                        ("fit", "same-speaker", positives),
                        ("fit", "different-speaker", negatives),
                        ("held-out", "same-speaker", (2.0,)),
                        ("held-out", "same-character/different-age", (0.0,)),
                    )
                    for distance in distances
                ]
                threshold, metrics = speaker_identity._fit_threshold(results)
                self.assertEqual(threshold, expected_threshold)
                self.assertEqual(
                    metrics,
                    {
                        "positive_count": len(positives),
                        "negative_count": len(negatives),
                        "max_positive_distance": max(positives, default=None),
                        "min_negative_distance": min(negatives, default=None),
                        "separated": expected_threshold is not None,
                    },
                )

    def test_report_separates_adjacent_fit_distances(self):
        with TemporaryDirectory() as directory:
            inventory = build_reference_inventory(_fixture(Path(directory)))
            ids = [item["reference_id"] for item in inventory["references"]]
            labels = build_labelled_pairs(
                inventory,
                [
                    {
                        "left_reference_id": ids[left],
                        "right_reference_id": ids[right],
                        "partition": partition,
                        "relationship": relationship,
                    }
                    for left, right, partition, relationship in (
                        (0, 1, "fit", "same-speaker"),
                        (0, 2, "fit", "different-speaker"),
                        (3, 4, "held-out", "same-speaker"),
                        (3, 5, "held-out", "different-speaker"),
                    )
                ],
            )
            vectors = {
                item["sha256"]: vector
                for item, vector in zip(
                    inventory["references"],
                    ((1, 0), (2**-53, 1), (0, 1), (1, 0), (1, 0), (0, 1)),
                    strict=True,
                )
            }
            report = build_speaker_identity_report(
                inventory,
                labels,
                lambda payload: vectors[hashlib.sha256(payload).hexdigest()],
                {},
            )
            self.assertEqual(report["threshold"], math.nextafter(1.0, 0.0))
            self.assertTrue(report["threshold_eligible"])
            self.assertEqual(report["held_out"]["boundary_violation_count"], 0)

    def test_invalid_model_embeddings_use_the_domain_error(self):
        with TemporaryDirectory() as directory:
            inventory = build_reference_inventory(_fixture(Path(directory)))
            left, right = [item["reference_id"] for item in inventory["references"][:2]]
            labels = build_labelled_pairs(
                inventory,
                [
                    {
                        "left_reference_id": left,
                        "right_reference_id": right,
                        "partition": "fit",
                        "relationship": "same-speaker",
                    }
                ],
            )
            for name, first, second in (
                ("nonnumeric", ("bad",), ("bad",)),
                ("ragged", ((1.0,), (1.0, 2.0)), (1.0, 0.0)),
                ("overflow", (10**1000,), (1.0, 0.0)),
                ("dimensions", (1.0, 0.0), (1.0, 0.0, 0.0)),
                ("empty", (), (1.0, 0.0)),
                ("nonfinite", (float("inf"),), (1.0, 0.0)),
                ("zero", (0.0, 0.0), (1.0, 0.0)),
            ):
                with self.subTest(embedding=name):
                    vectors = iter((first, second))
                    with self.assertRaisesRegex(SpeakerIdentityError, "embedding"):
                        build_speaker_identity_report(
                            inventory, labels, lambda payload: next(vectors), {}
                        )

    def test_invalid_evidence_json_uses_the_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = build_reference_inventory(_fixture(root))
            left, right = [item["reference_id"] for item in inventory["references"][:2]]
            labels = build_labelled_pairs(
                inventory,
                [
                    {
                        "left_reference_id": left,
                        "right_reference_id": right,
                        "partition": "fit",
                        "relationship": "same-speaker",
                    }
                ],
            )
            for name, document, load in (
                ("inventory", inventory, load_reference_inventory),
                ("labels", labels, lambda path: load_labelled_pairs(path, inventory)),
            ):
                with self.subTest(document=name):
                    path = root / f"{name}.json"
                    path.write_text(
                        json.dumps({**document, "unexpected": float("nan")}), "utf-8"
                    )
                    with self.assertRaisesRegex(
                        SpeakerIdentityError, "canonically hashed"
                    ):
                        load(path)
            with self.assertRaisesRegex(SpeakerIdentityError, "canonically hashed"):
                build_speaker_identity_report(
                    inventory, labels, lambda payload: (1.0, 0.0), {"invalid": object()}
                )

    def test_runtime_matches_the_declared_optional_dependency(self):
        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text("utf-8")
        )
        declared = project["project"]["optional-dependencies"]["speaker-identity"]
        self.assertEqual(len(declared), 1)
        package, separator, expected = declared[0].partition("==")
        self.assertEqual((package, separator), ("speechbrain", "=="))
        with patch.object(speaker_identity, "find_spec", return_value=object()):
            with patch.object(speaker_identity, "version", return_value=expected):
                require_speechbrain_runtime()
            for unsupported in ("1.0.3", "0.0.0"):
                with (
                    self.subTest(version=unsupported),
                    patch.object(speaker_identity, "version", return_value=unsupported),
                ):
                    with self.assertRaisesRegex(SpeakerIdentityError, "required"):
                        require_speechbrain_runtime()

    def test_runtime_rejects_non_cpu_before_loading_optional_dependency(self):
        with self.assertRaisesRegex(SpeakerIdentityError, "require CPU"):
            make_speechbrain_embedder("missing", device="cuda")

    def test_managed_model_verifies_every_allowlisted_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "model.bin").write_bytes(b"speaker model")
            digest = hashlib.sha256(b"speaker model").hexdigest()
            with patch.dict(MODEL_FILES, {"model.bin": digest}, clear=True):
                installed = install_managed_speaker_identity_model(
                    root=root / "managed", source=source
                )
                self.assertEqual(installed["status"], "installed")
                metadata = Path(
                    installed["installation"], "managed-model.json"
                ).read_bytes()
                notice = Path(
                    installed["installation"], "THIRD_PARTY_NOTICES.txt"
                ).read_bytes()
                self.assertEqual(
                    json.loads(metadata)["implementation"]["version"], "1.0.3"
                )
                with patch.object(
                    speaker_identity,
                    "managed_speaker_identity_status",
                    return_value=installed,
                ):
                    self.assertEqual(
                        speaker_identity.installed_model_descriptor()[
                            "implementation_version"
                        ],
                        "1.1.1",
                    )
                with patch.object(
                    managed_model_installation,
                    "file_sha256",
                    wraps=managed_model_installation.file_sha256,
                ) as hash_file:
                    repeated = install_managed_speaker_identity_model(
                        root=root / "managed", source=source
                    )
                hash_file.assert_called_once_with(
                    Path(installed["model_directory"], "model.bin"), error_type=OSError
                )
                self.assertEqual(repeated["status"], "installed")
                self.assertEqual(
                    Path(installed["installation"], "managed-model.json").read_bytes(),
                    metadata,
                )
                self.assertEqual(
                    Path(
                        installed["installation"], "THIRD_PARTY_NOTICES.txt"
                    ).read_bytes(),
                    notice,
                )
                self.assertEqual(
                    resolve_managed_speaker_identity_model(root=root / "managed"),
                    Path(installed["model_directory"]),
                )
                Path(installed["model_directory"], "model.bin").write_bytes(b"bad")
                self.assertEqual(
                    managed_speaker_identity_status(root=root / "managed")["status"],
                    "invalid",
                )
                with self.assertRaisesRegex(
                    SpeakerIdentityModelError, "Refusing to overwrite"
                ):
                    install_managed_speaker_identity_model(
                        root=root / "managed", source=source
                    )

    def test_cli_exposes_offline_diagnostic_commands(self):
        parser = create_parser()
        arguments = parser.parse_args(
            [
                "speaker-identity-evaluate",
                "inventory.json",
                "labels.json",
                "--output",
                "report.json",
                "--offline",
            ]
        )
        self.assertTrue(arguments.offline)
        self.assertEqual(arguments.device, "cpu")


if __name__ == "__main__":
    unittest.main()
