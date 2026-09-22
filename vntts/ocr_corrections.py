import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Self, TypeAlias

from vntts.ocr import OCRResult
from vntts.settings import get_config_directory
from vntts.versioned_json import load_versioned_json, write_versioned_json

corrections_schema_version = 1
PathInput: TypeAlias = str | Path
CorrectionEntries: TypeAlias = Mapping[str, str]


def get_ocr_corrections_path() -> Path:
    return Path(get_config_directory()) / "ocr-corrections.json"


class OCRCorrectionDictionary:
    def __init__(self, entries: object = None) -> None:
        self.entries = normalize_correction_entries(entries or {})

    def correct_result(self, result: OCRResult) -> OCRResult:
        character, character_changes = self.correct_text(result.character)
        text, text_changes = self.correct_text(result.text)
        changes = tuple(dict.fromkeys((*character_changes, *text_changes)))
        if not changes:
            return result
        return replace(
            result,
            character=character,
            text=text,
            corrections=changes,
        )

    def correct_text(self, value: str | None) -> tuple[str, tuple[str, ...]]:
        corrected = value or ""
        changes: list[str] = []
        entries = sorted(
            self.entries.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for source, replacement in entries:
            prefix = r"(?<!\w)" if source[0].isalnum() else ""
            suffix = r"(?!\w)" if source[-1].isalnum() else ""
            pattern = re.compile(f"{prefix}{re.escape(source)}{suffix}", re.IGNORECASE)
            corrected, count = pattern.subn(replacement, corrected)
            if count:
                changes.append(f"{source} -> {replacement}")
        return corrected, tuple(changes)


class OCRCorrectionStore:
    def __init__(
        self,
        path: PathInput | None = None,
        *,
        global_entries: object = None,
        profile_entries: Mapping[object, object] | None = None,
    ) -> None:
        self.path = (
            get_ocr_corrections_path() if path is None else Path(path).expanduser()
        )
        self.global_entries = normalize_correction_entries(global_entries or {})
        self.profile_entries = {
            str(profile_id): normalize_correction_entries(entries)
            for profile_id, entries in (profile_entries or {}).items()
        }

    @classmethod
    def load(
        cls,
        path: PathInput | None = None,
        *,
        warn: Callable[[str], None] | None = None,
    ) -> Self:
        report = (lambda _message: None) if warn is None else warn
        store = cls(path)

        def decode(payload: dict[str, object]) -> OCRCorrectionStore:
            store.global_entries = normalize_correction_entries(payload["global"])
            profiles = payload["profiles"]
            if not isinstance(profiles, dict):
                raise ValueError("OCR correction profiles must be a mapping")
            store.profile_entries = {
                str(profile_id): normalize_correction_entries(entries)
                for profile_id, entries in profiles.items()
            }
            return store

        def fallback() -> OCRCorrectionStore:
            store.global_entries = {}
            store.profile_entries = {}
            return store

        loaded = load_versioned_json(
            store.path,
            schema_version=corrections_schema_version,
            document_name="OCR corrections",
            decode=decode,
            fallback=fallback,
            warn=report,
        )
        if not isinstance(loaded, cls):
            raise TypeError("OCR correction loader returned an invalid store")
        return loaded

    def save(self) -> Path:
        self._save_entries(self.global_entries, self.profile_entries)
        return self.path

    def _save_entries(
        self,
        global_entries: dict[str, str],
        profile_entries: dict[str, dict[str, str]],
    ) -> None:
        write_versioned_json(
            self.path,
            corrections_schema_version,
            {
                "global": global_entries,
                "profiles": profile_entries,
            },
        )

    def dictionary_for(self, profile_id: str | None = None) -> OCRCorrectionDictionary:
        combined = dict(self.global_entries)
        if profile_id and profile_id in self.profile_entries:
            profile_keys = {key.casefold() for key in self.profile_entries[profile_id]}
            combined = {
                key: value
                for key, value in combined.items()
                if key.casefold() not in profile_keys
            }
            combined.update(self.profile_entries[profile_id])
        return OCRCorrectionDictionary(combined)

    def replace_entries(
        self,
        global_entries: object,
        profile_id: str | None = None,
        profile_entries: object = None,
    ) -> None:
        normalized_global = normalize_correction_entries(global_entries)
        normalized_profile = (
            normalize_correction_entries(profile_entries or {}) if profile_id else None
        )
        profiles = dict(self.profile_entries)
        if profile_id:
            if normalized_profile:
                profiles[str(profile_id)] = normalized_profile
            else:
                profiles.pop(str(profile_id), None)
        self._save_entries(normalized_global, profiles)
        self.global_entries = normalized_global
        self.profile_entries = profiles

    def upsert_entries(self, entries: object, profile_id: str | None = None) -> None:
        normalized = normalize_correction_entries(entries)
        profiles = dict(self.profile_entries)
        target = (
            profiles.get(str(profile_id), {}) if profile_id else self.global_entries
        )
        replaced_keys = {key.casefold() for key in normalized}
        merged = {
            source: replacement
            for source, replacement in target.items()
            if source.casefold() not in replaced_keys
        }
        merged.update(normalized)
        if profile_id:
            profiles[str(profile_id)] = merged
            global_entries = self.global_entries
        else:
            global_entries = merged
        self._save_entries(global_entries, profiles)
        self.global_entries = global_entries
        self.profile_entries = profiles

    def copy_profile(self, source_id: str, destination_id: str) -> None:
        entries = self.profile_entries.get(str(source_id))
        if entries:
            profiles = dict(self.profile_entries)
            profiles[str(destination_id)] = dict(entries)
            self._save_entries(self.global_entries, profiles)
            self.profile_entries = profiles

    def remove_profile(self, profile_id: str) -> None:
        if str(profile_id) in self.profile_entries:
            profiles = dict(self.profile_entries)
            profiles.pop(str(profile_id))
            self._save_entries(self.global_entries, profiles)
            self.profile_entries = profiles


def normalize_correction_entries(entries: object) -> dict[str, str]:
    if not isinstance(entries, dict):
        raise ValueError("OCR corrections must be a mapping")
    normalized: dict[str, str] = {}
    seen: set[str] = set()
    for source, replacement in entries.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("OCR correction source must not be empty")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError("OCR correction replacement must not be empty")
        source = source.strip()
        replacement = replacement.strip()
        key = source.casefold()
        if key in seen:
            raise ValueError(f"Duplicate OCR correction source: {source}")
        if source == replacement:
            raise ValueError(f"OCR correction does not change {source!r}")
        seen.add(key)
        normalized[source] = replacement
    return normalized
