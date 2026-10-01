import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.symlink_support import symlink_or_skip
from vntts.release_matrix import (
    load_evidence,
    load_release_matrix,
    validate_release_evidence,
)


class ReleaseMatrixTest(unittest.TestCase):
    def setUp(self):
        self.matrix_path = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "windows"
            / "release-matrix.json"
        )
        self.profiles = load_release_matrix(self.matrix_path)

    def evidence_for(self, profile):
        return {
            "success": True,
            "profile": profile["name"],
            "operating_system": "Microsoft Windows 11 Pro",
            "build_number": 26100,
            "gpu_vendor": profile["gpu_vendor"],
            "gpu_names": [f"{profile['gpu_vendor']} test adapter"],
            "display_count": profile["minimum_displays"],
            "monitor_index": 0,
            "dpi_scale_percent": profile["dpi_scale_percent"],
            "capture_mode": profile["capture_mode"],
            "game_process_level": profile["game_process_level"],
            "executable_signature": "Valid",
            "portable_archive_sha256": "a" * 64,
            "executable_signer_subject": "CN=VNTTS Release",
            "executable_signer_thumbprint": "c" * 40,
            "smoke_test_model": "tts_models/en/vctk/vits",
            "smoke_test_process_level": profile["game_process_level"],
            "auto_advance_dispatched": True,
            "auto_advance_acknowledged": True,
            "auto_advance_controller": "AppController._auto_advance_dialog",
        }

    def test_accepts_complete_matching_signed_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for profile in self.profiles:
                path = directory / f"{profile['name']}.json"
                path.write_text(
                    json.dumps(self.evidence_for(profile)),
                    encoding="utf-8",
                )

            reports = load_evidence(directory)

        self.assertEqual(
            validate_release_evidence(self.profiles, reports),
            [],
        )

    def test_evidence_loader_ignores_symlinked_reports(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = root / "evidence"
            evidence.mkdir()
            outside = root / "outside.json"
            outside.write_text(
                json.dumps(self.evidence_for(self.profiles[0])),
                encoding="utf-8",
            )
            symlink_or_skip(evidence / "linked.json", outside)

            reports = load_evidence(evidence)

        self.assertEqual(reports, [])

    def test_corrupt_utf8_does_not_hide_valid_release_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "corrupt.json").write_bytes(b"\xff")
            report_path = root / "valid.json"
            report = self.evidence_for(self.profiles[0])
            report_path.write_text(json.dumps(report), encoding="utf-8")

            self.assertEqual(load_evidence(root), [(report_path, report)])

    def test_powershell_utf8_bom_preserves_matrix_and_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            matrix_path = root / "matrix.json"
            matrix_path.write_text(
                json.dumps({"version": 1, "required_profiles": self.profiles}),
                encoding="utf-8-sig",
            )
            self.assertEqual(load_release_matrix(matrix_path), self.profiles)
            report = self.evidence_for(self.profiles[0])
            report["executable_signer_subject"] = "CN=Выпуск игры"
            report_path = root / "evidence.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8-sig"
            )
            self.assertEqual(load_evidence(root), [(report_path, report)])
            self.assertEqual(
                validate_release_evidence([self.profiles[0]], load_evidence(root)), []
            )

    def test_missing_requirements_cannot_match_missing_report_fields(self):
        with TemporaryDirectory() as temporary_directory:
            matrix_path = Path(temporary_directory) / "matrix.json"
            for field in (
                "gpu_vendor",
                "dpi_scale_percent",
                "capture_mode",
                "game_process_level",
                "minimum_displays",
            ):
                with self.subTest(field=field):
                    profile = dict(self.profiles[0])
                    report = self.evidence_for(profile)
                    del profile[field]
                    report.pop(field, None)
                    if field == "game_process_level":
                        report.pop("smoke_test_process_level")
                    errors = validate_release_evidence(
                        [profile], [(Path("incomplete.json"), report)]
                    )
                    self.assertTrue(any(field in error for error in errors))
                    matrix_path.write_text(
                        json.dumps({"version": 1, "required_profiles": [profile]}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, field):
                        load_release_matrix(matrix_path)

        self.assertTrue(validate_release_evidence([], []))

    def test_invalid_profile_requirements_are_rejected_before_reports(self):
        for field, invalid in (
            ("gpu_vendor", []),
            ("capture_mode", "unsupported"),
            ("game_process_level", None),
            ("dpi_scale_percent", True),
            ("minimum_displays", 0),
        ):
            with self.subTest(field=field):
                profile = dict(self.profiles[0], **{field: invalid})
                self.assertTrue(
                    any(
                        field in error
                        for error in validate_release_evidence([profile], [])
                    )
                )

    def test_environment_identity_is_text_and_legacy_integer_counts_still_work(self):
        profile = self.profiles[0]
        report = self.evidence_for(profile)
        report.update(
            build_number="26100", display_count=float(profile["minimum_displays"])
        )
        self.assertEqual(
            validate_release_evidence([profile], [(Path("valid.json"), report)]), []
        )
        report["operating_system"] = {"caption": "Windows 11"}
        errors = validate_release_evidence([profile], [(Path("bad-os.json"), report)])
        self.assertTrue(any("Windows 11" in error for error in errors))

    def test_rejects_missing_mismatched_and_unsigned_evidence(self):
        report = self.evidence_for(self.profiles[0])
        report["dpi_scale_percent"] = 200
        report["executable_signature"] = "NotSigned"
        reports = [(Path("bad.json"), report)]

        errors = validate_release_evidence(self.profiles, reports)

        self.assertTrue(any("dpi_scale_percent" in error for error in errors))
        self.assertTrue(any("signature" in error for error in errors))
        self.assertEqual(
            sum(error.startswith("Missing evidence") for error in errors),
            len(self.profiles) - 1,
        )

    def test_rejects_false_green_auto_advance_evidence(self):
        profile = self.profiles[0]
        report = self.evidence_for(profile)
        report["auto_advance_acknowledged"] = False
        report["auto_advance_controller"] = "legacy-smoke"

        errors = validate_release_evidence(
            [profile], [(Path("false-green.json"), report)]
        )

        self.assertTrue(any("not acknowledged" in error for error in errors))
        self.assertTrue(any("production controller" in error for error in errors))

    def test_unsigned_evidence_can_be_used_for_development(self):
        reports = []
        for profile in self.profiles:
            report = self.evidence_for(profile)
            report["executable_signature"] = "NotSigned"
            report["executable_signer_subject"] = None
            report["executable_signer_thumbprint"] = None
            reports.append((Path(f"{profile['name']}.json"), report))

        self.assertEqual(
            validate_release_evidence(
                self.profiles,
                reports,
                allow_unsigned=True,
            ),
            [],
        )

    def test_rejects_evidence_from_different_portable_artifacts(self):
        reports = []
        for index, profile in enumerate(self.profiles):
            report = self.evidence_for(profile)
            if index == 1:
                report["portable_archive_sha256"] = "d" * 64
            reports.append((Path(f"{profile['name']}.json"), report))

        errors = validate_release_evidence(self.profiles, reports)

        self.assertTrue(any("identical portable artifact" in error for error in errors))

    def test_rejects_non_integer_counts_and_malformed_profile_identity(self):
        profile = self.profiles[0]
        report = self.evidence_for(profile)
        report["display_count"] = True
        malformed = self.evidence_for(profile)
        malformed["profile"] = []

        errors = validate_release_evidence(
            [profile],
            [(Path("boolean-count.json"), report), (Path("bad-name.json"), malformed)],
            allow_unsigned=True,
        )

        self.assertTrue(any("display_count" in error for error in errors))
        self.assertTrue(any("identity is invalid" in error for error in errors))

    def test_malformed_matrix_root_and_profiles_are_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "matrix.json"
            for payload in (
                [],
                {"version": True, "required_profiles": [{}]},
                {"version": 1, "required_profiles": ["not-an-object"]},
                {"version": 2, "required_profiles": [{}]},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "root|profiles|version"):
                        load_release_matrix(path)


if __name__ == "__main__":
    unittest.main()
