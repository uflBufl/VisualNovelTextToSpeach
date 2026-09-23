"""Read-only suggestions for manual story-name links."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from vntts_artifacts.story_index import StoryIndexRecord
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.voices import is_narrator, synthesis_character_for_line


@dataclass(frozen=True)
class PersonLinkSuggestion:
    left_role: str
    right_role: str
    shared_portraits: tuple[str, ...]
    shared_source_banks: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "left_role": self.left_role,
            "right_role": self.right_role,
            "shared_portraits": list(self.shared_portraits),
            "shared_source_banks": list(self.shared_source_banks),
        }


def suggest_person_links(
    records: Iterable[StoryIndexRecord],
    existing_aliases: Mapping[str, str] | None = None,
) -> tuple[PersonLinkSuggestion, ...]:
    """Return conservative, player-confirmable links backed by source metadata."""
    roles: dict[str, _RoleEvidence] = {}
    for record in records:
        role = synthesis_character_for_line(record.speaker, record.voice_character)
        normalized = normalize_character_name(role)
        if not normalized or is_narrator(role):
            continue
        evidence = roles.setdefault(normalized, _RoleEvidence(role))
        evidence.remember_role(role)
        evidence.add(record)

    canonical_names = {
        normalize_character_name(alias): normalize_character_name(target)
        for alias, target in (existing_aliases or {}).items()
        if normalize_character_name(alias) and normalize_character_name(target)
    }
    ordered_roles = sorted(roles.items())
    suggestions: list[PersonLinkSuggestion] = []
    for index, (left_key, left) in enumerate(ordered_roles):
        for right_key, right in ordered_roles[index + 1 :]:
            if canonical_names.get(left_key, left_key) == canonical_names.get(
                right_key, right_key
            ):
                continue
            portraits = tuple(sorted(left.portraits & right.portraits))
            source_banks = tuple(
                sorted(left.voiced_source_banks & right.voiced_source_banks)
            )
            if len(portraits) >= 2 and source_banks:
                suggestions.append(
                    PersonLinkSuggestion(
                        left_role=left.role,
                        right_role=right.role,
                        shared_portraits=portraits,
                        shared_source_banks=source_banks,
                    )
                )
    return tuple(suggestions)


class _RoleEvidence:
    def __init__(self, role: str) -> None:
        self.role = role
        self.portraits: set[str] = set()
        self.voiced_source_banks: set[str] = set()

    def remember_role(self, role: str) -> None:
        if (role.casefold(), role) < (self.role.casefold(), self.role):
            self.role = role

    def add(self, record: StoryIndexRecord) -> None:
        portrait = _identifier(record.producer_fields.get("portrait"))
        if portrait is not None:
            self.portraits.add(portrait)
        source_bank = _identifier(record.producer_fields.get("source_bank"))
        source_voice_id = _identifier(record.source_audio_id)
        if source_bank is not None and source_voice_id is not None:
            self.voiced_source_banks.add(source_bank)


def _identifier(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = str(value).strip()
        return value or None
    return None
