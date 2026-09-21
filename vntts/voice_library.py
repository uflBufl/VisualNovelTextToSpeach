"""One application-owned, authoritative inventory of voice references."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from vntts_artifacts.atomic_io import atomic_output_path, atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import normalize_character_name

VOICE_LIBRARY_VERSION = 1
VoiceRoute = Literal["voice", "narrator", "live-fallback"]
_ROUTES = {"voice", "narrator", "live-fallback"}


class VoiceLibraryError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceAlternative:
    role: str
    variant_key: str | None
    sha256: str
    path: Path
    discovery: dict[str, object]


@dataclass(frozen=True)
class VoiceBinding:
    role: str
    variant_key: str | None
    route: VoiceRoute
    source_sha256s: tuple[str, ...]
    source_id: str | None
    provenance: dict[str, object]

    @property
    def source_sha256(self) -> str | None:
        """Compatibility convenience for bindings with one WAV reference."""
        return self.source_sha256s[0] if len(self.source_sha256s) == 1 else None


class VoiceLibrary:
    """Durable voice choices at a caller-selected fixed directory or JSON path."""

    def __init__(self, path: str | Path) -> None:
        requested = Path(path).expanduser().absolute()
        if requested.suffix:
            self.root = requested.parent.resolve(strict=False)
            self.path = self.root / requested.name
        else:
            self.root = requested.resolve(strict=False)
            self.path = self.root / "voice-library.json"
        self.blobs_path = self.root / "voice-blobs"

    def discover(
        self,
        role: str,
        reference: str | Path,
        *,
        variant_key: str | None = None,
        method: Literal["automatic", "manual"] = "automatic",
        evidence: object | None = None,
        algorithm: str | None = None,
        timestamp: str | None = None,
        bind_if_missing: bool = False,
    ) -> VoiceAlternative:
        """Store a WAV alternative; optionally bind it only when no choice exists."""
        identity, display_role, display_variant = _role_identity(role, variant_key)
        payload = _read_wav(reference)
        checksum = hashlib.sha256(payload).hexdigest()
        self._store_blob(checksum, payload)
        document = self._load()
        group = document["alternatives"].setdefault(
            identity,
            {"role": display_role, "variant_key": display_variant, "items": []},
        )
        if not any(item["sha256"] == checksum for item in group["items"]):
            group["items"].append(
                {
                    "sha256": checksum,
                    "discovery": _provenance(method, evidence, algorithm, timestamp),
                }
            )
            group["items"].sort(key=lambda item: item["sha256"])
        if bind_if_missing and identity not in document["bindings"]:
            document["bindings"][identity] = _binding_document(
                display_role,
                display_variant,
                "voice",
                (checksum,),
                None,
                _provenance(method, evidence, algorithm, timestamp),
            )
        self._write(document)
        return VoiceAlternative(
            display_role,
            display_variant,
            checksum,
            self._blob_path(checksum),
            next(
                item["discovery"]
                for item in document["alternatives"][identity]["items"]
                if item["sha256"] == checksum
            ),
        )

    def select(
        self,
        role: str,
        *,
        route: VoiceRoute,
        variant_key: str | None = None,
        source_sha256: str | None = None,
        source_sha256s: Iterable[str] | None = None,
        source_id: str | None = None,
        method: Literal["automatic", "manual"] = "manual",
        evidence: object | None = None,
        algorithm: str | None = None,
        timestamp: str | None = None,
        only_if_unbound: bool = False,
    ) -> VoiceBinding:
        """Atomically replace one choice, or leave it intact when requested."""
        identity, display_role, display_variant = _role_identity(role, variant_key)
        document = self._load()
        current = document["bindings"].get(identity)
        if only_if_unbound and current is not None:
            return _to_binding(current)
        selected_checksums = _selected_checksums(source_sha256, source_sha256s)
        _validate_route_source(route, selected_checksums, source_id)
        if selected_checksums:
            alternatives = document["alternatives"].get(identity, {}).get("items", [])
            available = {item["sha256"] for item in alternatives}
            if any(checksum not in available for checksum in selected_checksums):
                raise VoiceLibraryError(
                    "Selected voice is not an alternative for this role"
                )
            for checksum in selected_checksums:
                self._validate_blob(checksum)
        binding = _binding_document(
            display_role,
            display_variant,
            route,
            selected_checksums,
            source_id,
            _provenance(method, evidence, algorithm, timestamp),
        )
        document["bindings"][identity] = binding
        self._write(document)
        return _to_binding(binding)

    def binding(
        self, role: str, *, variant_key: str | None = None
    ) -> VoiceBinding | None:
        identity, _role, _variant = _role_identity(role, variant_key)
        raw = self._load()["bindings"].get(identity)
        return _to_binding(raw) if raw is not None else None

    def bindings(self) -> tuple[VoiceBinding, ...]:
        return tuple(
            _to_binding(item)
            for _identity, item in sorted(self._load()["bindings"].items())
        )

    def clear(self, role: str, *, variant_key: str | None = None) -> bool:
        """Remove one explicit decision while keeping its alternatives."""
        identity, _role, _variant = _role_identity(role, variant_key)
        document = self._load()
        if document["bindings"].pop(identity, None) is None:
            return False
        self._write(document)
        return True

    def alternatives(
        self, role: str, *, variant_key: str | None = None
    ) -> tuple[VoiceAlternative, ...]:
        identity, _role, _variant = _role_identity(role, variant_key)
        group = self._load()["alternatives"].get(identity)
        if group is None:
            return ()
        return tuple(
            VoiceAlternative(
                group["role"],
                group["variant_key"],
                item["sha256"],
                self._blob_path(item["sha256"]),
                item["discovery"],
            )
            for item in group["items"]
        )

    def resolve_source_paths(
        self, role: str, *, variant_key: str | None = None
    ) -> tuple[Path, ...]:
        """Return selected local WAVs in their binding order."""
        binding = self.binding(role, variant_key=variant_key)
        if binding is None:
            return ()
        return tuple(
            self._validate_blob(checksum) for checksum in binding.source_sha256s
        )

    def resolve_source_path(
        self, role: str, *, variant_key: str | None = None
    ) -> Path | None:
        """Return a selected local WAV only when the binding has exactly one."""
        paths = self.resolve_source_paths(role, variant_key=variant_key)
        return paths[0] if len(paths) == 1 else None

    def validate(self) -> None:
        """Check every recorded blob and every binding reference."""
        document = self._load()
        for group in document["alternatives"].values():
            for item in group["items"]:
                self._validate_blob(item["sha256"])
        for binding in document["bindings"].values():
            for checksum in binding["source_sha256s"]:
                self._validate_blob(checksum)

    def copy_to(self, path: str | Path) -> VoiceLibrary:
        """Copy the complete library to a new, unused directory."""
        target = VoiceLibrary(path)
        if target.root.exists():
            raise VoiceLibraryError("Voice library copy destination already exists")
        self.validate()
        if self.path.exists():
            shutil.copytree(self.root, target.root)
        return target

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "version": VOICE_LIBRARY_VERSION,
                "alternatives": {},
                "bindings": {},
            }
        if self.path.is_symlink():
            raise VoiceLibraryError("Voice library index must not be a symlink")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise VoiceLibraryError(f"Unable to read voice library: {error}") from error
        _validate_document(document)
        return document

    def _write(self, document: dict[str, object]) -> None:
        _validate_document(document)
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, document, sort_keys=True)

    def _blob_path(self, checksum: str) -> Path:
        if self.blobs_path.is_symlink():
            raise VoiceLibraryError("Voice blob directory must not be a symlink")
        return self.blobs_path / f"{checksum}.wav"

    def _store_blob(self, checksum: str, payload: bytes) -> Path:
        destination = self._blob_path(checksum)
        if destination.exists():
            return self._validate_blob(checksum)
        self.blobs_path.mkdir(parents=True, exist_ok=True)
        with atomic_output_path(destination) as staged:
            staged.write_bytes(payload)
        return self._validate_blob(checksum)

    def _validate_blob(self, checksum: str) -> Path:
        if not _is_sha256(checksum):
            raise VoiceLibraryError("Voice blob checksum must be lowercase SHA-256")
        path = self._blob_path(checksum)
        if path.is_symlink() or not path.is_file():
            raise VoiceLibraryError(f"Voice blob is missing or unsafe: {checksum}")
        if sha256_file(path) != checksum:
            raise VoiceLibraryError(f"Voice blob checksum failed: {checksum}")
        try:
            with wave.open(str(path), "rb"):
                pass
        except (OSError, wave.Error) as error:
            raise VoiceLibraryError(
                f"Voice blob is not a WAV file: {checksum}"
            ) from error
        return path


def _role_identity(role: str, variant_key: str | None) -> tuple[str, str, str | None]:
    if not isinstance(role, str) or not (display_role := role.strip()):
        raise VoiceLibraryError("Voice role is required")
    normalized_role = normalize_character_name(display_role)
    if not normalized_role:
        raise VoiceLibraryError("Voice role must contain letters or numbers")
    if variant_key is not None and (
        not isinstance(variant_key, str) or not variant_key.strip()
    ):
        raise VoiceLibraryError("Voice variant key must be non-empty text")
    display_variant = variant_key.strip() if variant_key is not None else None
    normalized_variant = normalize_character_name(display_variant or "")
    if display_variant is not None and not normalized_variant:
        raise VoiceLibraryError("Voice variant key must contain letters or numbers")
    return f"{normalized_role}:{normalized_variant}", display_role, display_variant


def _read_wav(reference: str | Path) -> bytes:
    path = Path(reference).expanduser()
    if path.suffix.casefold() != ".wav":
        raise VoiceLibraryError("Voice reference must be a WAV file")
    if path.is_symlink():
        raise VoiceLibraryError("Voice reference must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise VoiceLibraryError(f"Unable to open voice reference: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise VoiceLibraryError("Voice reference must be a regular file")
        payload = b"".join(iter(lambda: os.read(descriptor, 1024 * 1024), b""))
        current = path.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ) or len(payload) != opened.st_size:
            raise VoiceLibraryError("Voice reference changed while it was read")
    finally:
        os.close(descriptor)
    try:
        with wave.open(io.BytesIO(payload), "rb"):
            pass
    except wave.Error as error:
        raise VoiceLibraryError("Voice reference is not a WAV file") from error
    return payload


def _provenance(
    method: str, evidence: object | None, algorithm: str | None, timestamp: str | None
) -> dict[str, object]:
    if method not in {"automatic", "manual"}:
        raise VoiceLibraryError("Voice provenance method must be automatic or manual")
    if algorithm is not None and (
        not isinstance(algorithm, str) or not algorithm.strip()
    ):
        raise VoiceLibraryError("Voice algorithm must be non-empty text")
    if timestamp is not None and (
        not isinstance(timestamp, str) or not timestamp.strip()
    ):
        raise VoiceLibraryError("Voice timestamp must be non-empty text")
    try:
        safe_evidence = json.loads(json.dumps(evidence))
    except (TypeError, ValueError) as error:
        raise VoiceLibraryError("Voice evidence must be JSON data") from error
    return {
        "method": method,
        "evidence": safe_evidence,
        "algorithm": algorithm.strip() if algorithm else None,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


def _binding_document(
    role: str,
    variant_key: str | None,
    route: VoiceRoute,
    source_sha256s: tuple[str, ...],
    source_id: str | None,
    provenance: dict[str, object],
) -> dict[str, object]:
    _validate_route_source(route, source_sha256s, source_id)
    return {
        "role": role,
        "variant_key": variant_key,
        "route": route,
        "source_sha256s": list(source_sha256s),
        "source_id": source_id,
        "provenance": provenance,
    }


def _validate_route_source(
    route: str,
    source_sha256s: tuple[str, ...],
    source_id: str | None,
) -> None:
    if route not in _ROUTES:
        raise VoiceLibraryError("Voice route must be voice, narrator, or live-fallback")
    if route == "voice":
        if bool(source_sha256s) == (source_id is not None):
            raise VoiceLibraryError("Voice route requires exactly one source")
        if source_id is not None and (
            not isinstance(source_id, str) or not source_id.strip()
        ):
            raise VoiceLibraryError("External voice source ID must be non-empty text")
    elif source_sha256s or source_id is not None:
        raise VoiceLibraryError(f"{route} route cannot have a voice source")


def _to_binding(raw: dict[str, object]) -> VoiceBinding:
    return VoiceBinding(
        raw["role"],
        raw["variant_key"],
        raw["route"],
        tuple(raw["source_sha256s"]),
        raw["source_id"],
        raw["provenance"],
    )


def _selected_checksums(
    source_sha256: str | None, source_sha256s: Iterable[str] | None
) -> tuple[str, ...]:
    if source_sha256 is not None and source_sha256s is not None:
        raise VoiceLibraryError("Provide source_sha256 or source_sha256s, not both")
    values = (
        (source_sha256,) if source_sha256 is not None else tuple(source_sha256s or ())
    )
    if not values or any(not _is_sha256(value) for value in values):
        if values:
            raise VoiceLibraryError("Voice source checksum must be lowercase SHA-256")
        return ()
    if len(set(values)) != len(values):
        raise VoiceLibraryError("Voice source checksums must be unique")
    return values


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_document(document: object) -> None:
    if (
        not isinstance(document, dict)
        or document.get("version") != VOICE_LIBRARY_VERSION
    ):
        raise VoiceLibraryError("Unsupported voice library document")
    alternatives = document.get("alternatives")
    bindings = document.get("bindings")
    if not isinstance(alternatives, dict) or not isinstance(bindings, dict):
        raise VoiceLibraryError("Voice library requires alternatives and bindings")
    for identity, group in alternatives.items():
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            raise VoiceLibraryError("Invalid voice alternative inventory")
        expected, _role, _variant = _role_identity(
            group.get("role"), group.get("variant_key")
        )
        if identity != expected:
            raise VoiceLibraryError("Voice alternative role identity is invalid")
        checksums = [
            item.get("sha256") for item in group["items"] if isinstance(item, dict)
        ]
        if len(checksums) != len(group["items"]) or len(set(checksums)) != len(
            checksums
        ):
            raise VoiceLibraryError("Voice alternatives must have unique checksums")
        for item in group["items"]:
            if not _is_sha256(item["sha256"]):
                raise VoiceLibraryError("Voice alternative checksum is invalid")
            _validate_provenance(item.get("discovery"))
    for identity, binding in bindings.items():
        if not isinstance(binding, dict):
            raise VoiceLibraryError("Invalid voice binding")
        expected, _role, _variant = _role_identity(
            binding.get("role"), binding.get("variant_key")
        )
        if identity != expected:
            raise VoiceLibraryError("Voice binding role identity is invalid")
        raw_checksums = binding.get("source_sha256s")
        if (
            not isinstance(raw_checksums, list)
            or any(not _is_sha256(checksum) for checksum in raw_checksums)
            or len(set(raw_checksums)) != len(raw_checksums)
        ):
            raise VoiceLibraryError("Voice binding checksums are invalid")
        _validate_route_source(
            binding.get("route"), tuple(raw_checksums), binding.get("source_id")
        )
        available = {
            item["sha256"] for item in alternatives.get(identity, {}).get("items", [])
        }
        if any(checksum not in available for checksum in raw_checksums):
            raise VoiceLibraryError(
                "Voice binding source is not an alternative for its role"
            )
        _validate_provenance(binding.get("provenance"))


def _validate_provenance(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "method",
        "evidence",
        "algorithm",
        "timestamp",
    }:
        raise VoiceLibraryError("Voice provenance is invalid")
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise VoiceLibraryError("Voice provenance timestamp is invalid")
    _provenance(
        value.get("method"),
        value.get("evidence"),
        value.get("algorithm"),
        timestamp,
    )


__all__ = [
    "VOICE_LIBRARY_VERSION",
    "VoiceAlternative",
    "VoiceBinding",
    "VoiceLibrary",
    "VoiceLibraryError",
]
