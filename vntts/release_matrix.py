import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TypeAlias

PathInput: TypeAlias = str | os.PathLike[str]
ReleaseDocument: TypeAlias = dict[str, object]
ReleaseEvidence: TypeAlias = tuple[Path, ReleaseDocument]


def _report_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 0
    if isinstance(value, float) and not value.is_integer():
        return 0
    try:
        return int(value)
    except OverflowError, ValueError:
        return 0


def load_release_matrix(path: PathInput) -> list[ReleaseDocument]:
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("Release matrix root must be an object")
    if type(values.get("version")) is not int or values.get("version") != 1:
        raise ValueError("Unsupported release matrix version")
    profiles: object = values.get("required_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Release matrix must contain required_profiles")
    if not all(isinstance(profile, dict) for profile in profiles):
        raise ValueError("Release matrix profiles must be objects")
    return [profile for profile in profiles if isinstance(profile, dict)]


def load_evidence(directory: PathInput) -> list[ReleaseEvidence]:
    reports: list[ReleaseEvidence] = []
    for path in sorted(Path(directory).rglob("*.json")):
        if path.is_symlink():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "profile" in report:
            reports.append((path, report))
    return reports


def validate_release_evidence(
    profiles: Sequence[ReleaseDocument],
    reports: Sequence[ReleaseEvidence],
    *,
    allow_unsigned: bool = False,
) -> list[str]:
    errors: list[str] = []
    required: dict[str, ReleaseDocument] = {}
    for position, profile in enumerate(profiles, start=1):
        if not isinstance(profile, dict):
            errors.append(f"Release profile {position} is not an object")
            continue
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"Release profile {position} has an invalid name")
            continue
        if name in required:
            errors.append(f"Release profile name is duplicated: {name!r}")
            continue
        required[name] = profile
    evidence = {}
    artifact_bindings = set()
    for path, report in reports:
        name = report.get("profile")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{path}: release profile identity is invalid")
            continue
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
        build_number = _report_integer(report.get("build_number", 0))
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
        display_count = _report_integer(report.get("display_count", 0))
        minimum_displays = _report_integer(profile.get("minimum_displays"))
        if minimum_displays < 1:
            errors.append(f"{prefix} release profile minimum_displays is invalid")
        elif display_count < minimum_displays:
            errors.append(
                f"{prefix} display_count is {display_count}, expected at least "
                f"{profile['minimum_displays']}"
            )
        if not allow_unsigned and report.get("executable_signature") != "Valid":
            errors.append(f"{prefix} executable signature is not valid")
        archive_sha256 = report.get("portable_archive_sha256")
        if not isinstance(archive_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", archive_sha256
        ):
            errors.append(f"{prefix} portable archive SHA-256 is missing or invalid")
        signer_subject = report.get("executable_signer_subject")
        signer_thumbprint = report.get("executable_signer_thumbprint")
        if not allow_unsigned and (
            not isinstance(signer_subject, str)
            or not signer_subject.strip()
            or not isinstance(signer_thumbprint, str)
            or not re.fullmatch(r"[0-9a-f]{40}", signer_thumbprint)
        ):
            errors.append(f"{prefix} executable signer identity is missing or invalid")
        artifact_bindings.add(
            tuple(
                value if isinstance(value, str) else None
                for value in (archive_sha256, signer_subject, signer_thumbprint)
            )
        )
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
            "Release profiles do not describe one identical portable artifact"
        )
    return errors
