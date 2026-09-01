import json
import re
from pathlib import Path


def load_release_matrix(path):
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = values.get("required_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Release matrix must contain required_profiles")
    return profiles


def load_evidence(directory):
    reports = []
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(report, dict) and "profile" in report:
            reports.append((path, report))
    return reports


def validate_release_evidence(profiles, reports, *, allow_unsigned=False):
    errors = []
    required = {profile["name"]: profile for profile in profiles}
    evidence = {}
    artifact_bindings = set()
    for path, report in reports:
        name = report.get("profile")
        if name not in required:
            errors.append(f"{path}: unknown release profile {name!r}")
            continue
        if name in evidence:
            errors.append(f"{path}: duplicate evidence for profile {name!r}")
            continue
        evidence[name] = (path, report)

    for name, profile in required.items():
        if name not in evidence:
            errors.append(f"Missing evidence for profile {name!r}")
            continue
        path, report = evidence[name]
        prefix = f"{path}:"
        if report.get("success") is not True:
            errors.append(f"{prefix} release test did not succeed")
        if "Windows 11" not in str(report.get("operating_system", "")):
            errors.append(f"{prefix} test did not run on Windows 11")
        try:
            build_number = int(report.get("build_number", 0))
        except (TypeError, ValueError):
            build_number = 0
        if build_number < 22000:
            errors.append(f"{prefix} Windows build is older than 22000")

        exact_fields = {
            "gpu_vendor": "gpu_vendor",
            "dpi_scale_percent": "dpi_scale_percent",
            "capture_mode": "capture_mode",
            "game_process_level": "game_process_level",
        }
        for report_field, profile_field in exact_fields.items():
            if report.get(report_field) != profile.get(profile_field):
                errors.append(
                    f"{prefix} {report_field} is {report.get(report_field)!r}, "
                    f"expected {profile.get(profile_field)!r}"
                )
        try:
            display_count = int(report.get("display_count", 0))
        except (TypeError, ValueError):
            display_count = 0
        if display_count < int(profile["minimum_displays"]):
            errors.append(
                f"{prefix} display_count is {display_count}, expected at least "
                f"{profile['minimum_displays']}"
            )
        if not allow_unsigned and report.get("installer_signature") != "Valid":
            errors.append(f"{prefix} installer signature is not valid")
        installer_sha256 = report.get("installer_sha256")
        if not isinstance(installer_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", installer_sha256
        ):
            errors.append(f"{prefix} installer SHA-256 is missing or invalid")
        installer_version = report.get("installer_product_version")
        if not isinstance(installer_version, str) or not installer_version.strip():
            errors.append(f"{prefix} installer product version is missing")
        signer_subject = report.get("installer_signer_subject")
        signer_thumbprint = report.get("installer_signer_thumbprint")
        if not allow_unsigned and (
            not isinstance(signer_subject, str)
            or not signer_subject.strip()
            or not isinstance(signer_thumbprint, str)
            or not re.fullmatch(r"[0-9a-f]{40}", signer_thumbprint)
        ):
            errors.append(f"{prefix} installer signer identity is missing or invalid")
        artifact_bindings.add(
            (installer_sha256, installer_version, signer_subject, signer_thumbprint)
        )
        previous_sha256 = report.get("previous_installer_sha256")
        if not isinstance(previous_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", previous_sha256
        ):
            errors.append(f"{prefix} previous installer SHA-256 is missing or invalid")
        if previous_sha256 == installer_sha256:
            errors.append(f"{prefix} upgrade reused the candidate installer")
        previous_version = report.get("previous_installer_product_version")
        if not isinstance(previous_version, str) or not previous_version.strip():
            errors.append(f"{prefix} previous installer product version is missing")
        if report.get("upgrade_verified") is not True:
            errors.append(f"{prefix} upgrade from a previous artifact was not verified")
        if report.get("smoke_test_process_level") != profile.get("game_process_level"):
            errors.append(
                f"{prefix} smoke test did not match the game process integrity level"
            )
        if report.get("auto_advance_dispatched") is not True:
            errors.append(f"{prefix} production auto advance was not dispatched")
        if report.get("auto_advance_acknowledged") is not True:
            errors.append(f"{prefix} auto advance was not acknowledged by the fixture")
        if (
            report.get("auto_advance_controller")
            != "AppController._auto_advance_dialog"
        ):
            errors.append(f"{prefix} auto advance bypassed the production controller")
    if len(artifact_bindings) > 1:
        errors.append(
            "Release profiles do not describe one identical installer artifact"
        )
    return errors
