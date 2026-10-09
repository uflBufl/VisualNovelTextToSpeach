import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TypeAlias

from vntts.document_identity import is_lowercase_sha256
from vntts.json_types import decode_json

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
    values = decode_json(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise ValueError("Release matrix root must be an object")
    if type(values.get("version")) is not int or values.get("version") != 1:
        raise ValueError("Unsupported release matrix version")
    profiles: object = values.get("required_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Release matrix must contain required_profiles")
    required, errors = _required_release_profiles(profiles)
    if errors:
        raise ValueError("Invalid release matrix profiles: " + "; ".join(errors))
    return list(required.values())


def load_evidence(directory: PathInput) -> list[ReleaseEvidence]:
    reports: list[ReleaseEvidence] = []
    for path in sorted(Path(directory).rglob("*.json")):
        if path.is_symlink():
            continue
        try:
            report = decode_json(path.read_text(encoding="utf-8-sig"))
        except OSError, UnicodeError, json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "profile" in report:
            reports.append((path, report))
    return reports


def _required_release_profiles(
    profiles: Sequence[object],
) -> tuple[dict[str, ReleaseDocument], list[str]]:
    errors: list[str] = []
    required: dict[str, ReleaseDocument] = {}
    if not profiles:
        errors.append("Release matrix must contain required_profiles")
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
        for field, allowed in (
            ("gpu_vendor", ("Intel", "NVIDIA", "AMD")),
            ("capture_mode", ("windowed", "borderless")),
            ("game_process_level", ("normal", "elevated")),
        ):
            if profile.get(field) not in allowed:
                errors.append(f"Release profile {name!r} has an invalid {field}")
        for field in ("dpi_scale_percent", "minimum_displays"):
            if _report_integer(profile.get(field)) < 1:
                errors.append(f"Release profile {name!r} has an invalid {field}")
    return required, errors


def _profile_evidence_errors(
    profile: ReleaseDocument,
    path: Path,
    report: ReleaseDocument,
    *,
    allow_unsigned: bool,
) -> list[str]:
    prefix = f"{path}:"
    operating_system = report.get("operating_system")
    checks = (
        (report.get("success") is True, "release test did not succeed"),
        (
            isinstance(operating_system, str) and "Windows 11" in operating_system,
            "test did not run on Windows 11",
        ),
        (
            _report_integer(report.get("build_number")) >= 22000,
            "Windows build is older than 22000",
        ),
        (
            report.get("smoke_test_process_level") == profile["game_process_level"],
            "smoke test did not match the game process integrity level",
        ),
        (
            report.get("auto_advance_dispatched") is True,
            "production auto advance was not dispatched",
        ),
        (
            report.get("auto_advance_acknowledged") is True,
            "auto advance was not acknowledged by the fixture",
        ),
        (
            report.get("auto_advance_controller")
            == "AppController._auto_advance_dialog",
            "auto advance bypassed the production controller",
        ),
    )
    errors = [f"{prefix} {message}" for passed, message in checks if not passed]
    for field in (
        "gpu_vendor",
        "dpi_scale_percent",
        "capture_mode",
        "game_process_level",
    ):
        if report.get(field) != profile[field]:
            errors.append(
                f"{prefix} {field} is {report.get(field)!r}, expected {profile[field]!r}"
            )
    display_count = _report_integer(report.get("display_count"))
    if display_count < _report_integer(profile["minimum_displays"]):
        errors.append(
            f"{prefix} display_count is {display_count}, expected at least "
            f"{profile['minimum_displays']}"
        )
    if not allow_unsigned and report.get("executable_signature") != "Valid":
        errors.append(f"{prefix} executable signature is not valid")
    if not is_lowercase_sha256(report.get("portable_archive_sha256")):
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
    return errors


def validate_release_evidence(
    profiles: Sequence[ReleaseDocument],
    reports: Sequence[ReleaseEvidence],
    *,
    allow_unsigned: bool = False,
) -> list[str]:
    required, errors = _required_release_profiles(profiles)
    if errors:
        return errors
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
        errors.extend(
            _profile_evidence_errors(
                profile, path, report, allow_unsigned=allow_unsigned
            )
        )
        artifact_bindings.add(
            tuple(
                value if isinstance(value, str) else None
                for value in (
                    report.get("portable_archive_sha256"),
                    report.get("executable_signer_subject"),
                    report.get("executable_signer_thumbprint"),
                )
            )
        )
    if len(artifact_bindings) > 1:
        errors.append(
            "Release profiles do not describe one identical portable artifact"
        )
    return errors
