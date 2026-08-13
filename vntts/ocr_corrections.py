import json
import re
from dataclasses import replace
from pathlib import Path

from vntts.atomic_io import atomic_write_json
from vntts.settings import get_config_directory

corrections_schema_version = 1


def get_ocr_corrections_path():
    return get_config_directory() / "ocr-corrections.json"


class OCRCorrectionDictionary:
    def __init__(self, entries=None):
        self.entries = normalize_correction_entries(entries or {})

    def correct_result(self, result):
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

    def correct_text(self, value):
        corrected = value or ""
        changes = []
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
    def __init__(self, path=None, *, global_entries=None, profile_entries=None):
        self.path = (
            get_ocr_corrections_path() if path is None else Path(path).expanduser()
        )
        self.global_entries = normalize_correction_entries(global_entries or {})
        self.profile_entries = {
            str(profile_id): normalize_correction_entries(entries)
            for profile_id, entries in (profile_entries or {}).items()
        }

    @classmethod
    def load(cls, path=None, *, warn=None):
        warn = (lambda _message: None) if warn is None else warn
        store = cls(path)
        if not store.path.is_file():
            return store
        try:
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != corrections_schema_version:
                raise ValueError("unsupported OCR corrections schema version")
            store.global_entries = normalize_correction_entries(payload["global"])
            store.profile_entries = {
                str(profile_id): normalize_correction_entries(entries)
                for profile_id, entries in payload["profiles"].items()
            }
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            warn(f"Unable to load OCR corrections from {store.path}: {error}")
            store.global_entries = {}
            store.profile_entries = {}
        return store

    def save(self):
        atomic_write_json(
            self.path,
            {
                "schema_version": corrections_schema_version,
                "global": self.global_entries,
                "profiles": self.profile_entries,
            },
        )
        return self.path

    def dictionary_for(self, profile_id=None):
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

    def replace_entries(self, global_entries, profile_id=None, profile_entries=None):
        normalized_global = normalize_correction_entries(global_entries)
        normalized_profile = (
            normalize_correction_entries(profile_entries or {}) if profile_id else None
        )
        self.global_entries = normalized_global
        if profile_id:
            if normalized_profile:
                self.profile_entries[str(profile_id)] = normalized_profile
            else:
                self.profile_entries.pop(str(profile_id), None)
        self.save()

    def upsert_entries(self, entries, profile_id=None):
        normalized = normalize_correction_entries(entries)
        target = (
            self.profile_entries.setdefault(str(profile_id), {})
            if profile_id
            else self.global_entries
        )
        replaced_keys = {key.casefold() for key in normalized}
        merged = {
            source: replacement
            for source, replacement in target.items()
            if source.casefold() not in replaced_keys
        }
        merged.update(normalized)
        if profile_id:
            self.profile_entries[str(profile_id)] = merged
        else:
            self.global_entries = merged
        self.save()

    def copy_profile(self, source_id, destination_id):
        entries = self.profile_entries.get(str(source_id))
        if entries:
            self.profile_entries[str(destination_id)] = dict(entries)
            self.save()

    def remove_profile(self, profile_id):
        if self.profile_entries.pop(str(profile_id), None) is not None:
            self.save()


def normalize_correction_entries(entries):
    if not isinstance(entries, dict):
        raise ValueError("OCR corrections must be a mapping")
    normalized = {}
    seen = set()
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
