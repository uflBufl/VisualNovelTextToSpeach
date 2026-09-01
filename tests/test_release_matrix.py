import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
            "installer_signature": "Valid",
            "installer_sha256": "a" * 64,
            "installer_product_version": "0.2.0",
            "installer_signer_subject": "CN=VNTTS Release",
            "installer_signer_thumbprint": "c" * 40,
            "previous_installer_sha256": "b" * 64,
            "previous_installer_product_version": "0.1.0",
            "upgrade_verified": True,
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

    def test_rejects_missing_mismatched_and_unsigned_evidence(self):
        report = self.evidence_for(self.profiles[0])
        report["dpi_scale_percent"] = 200
        report["installer_signature"] = "NotSigned"
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
            report["installer_signature"] = "NotSigned"
            report["installer_signer_subject"] = None
            report["installer_signer_thumbprint"] = None
            reports.append((Path(f"{profile['name']}.json"), report))

        self.assertEqual(
            validate_release_evidence(
                self.profiles,
                reports,
                allow_unsigned=True,
            ),
            [],
        )

    def test_rejects_evidence_from_different_candidate_installers(self):
        reports = []
        for index, profile in enumerate(self.profiles):
            report = self.evidence_for(profile)
            if index == 1:
                report["installer_sha256"] = "d" * 64
            reports.append((Path(f"{profile['name']}.json"), report))

        errors = validate_release_evidence(self.profiles, reports)

        self.assertTrue(
            any("identical installer artifact" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
