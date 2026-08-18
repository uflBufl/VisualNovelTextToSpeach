"""Explicit immutable authoring policy for unresolved named voice roles."""

from __future__ import annotations

from dataclasses import dataclass

from vntts_artifacts.voice_manifest import normalize_character_name

MISSING_VOICE_POLICY_VERSION = 1
BLOCK_MISSING_VOICE = "block"
NARRATOR_ROLES = "narrator_roles"
NARRATOR_ALL_UNRESOLVED = "narrator_all_unresolved"
MISSING_VOICE_POLICY_MODES = {
    BLOCK_MISSING_VOICE,
    NARRATOR_ROLES,
    NARRATOR_ALL_UNRESOLVED,
}


class MissingVoicePolicyError(ValueError):
    """A missing-voice policy is unsafe or malformed."""


@dataclass(frozen=True)
class MissingVoicePolicy:
    mode: str = BLOCK_MISSING_VOICE
    roles: tuple[str, ...] = ()

    def __post_init__(self):
        if self.mode not in MISSING_VOICE_POLICY_MODES:
            raise MissingVoicePolicyError(
                f"Unsupported missing-voice mode: {self.mode!r}"
            )
        normalized = {}
        for role in self.roles:
            if not isinstance(role, str) or not role.strip():
                raise MissingVoicePolicyError(
                    "Missing-voice roles must be non-empty text"
                )
            cleaned = role.strip()
            key = normalize_character_name(cleaned)
            if not key:
                raise MissingVoicePolicyError(
                    f"Missing-voice role is invalid: {role!r}"
                )
            previous = normalized.get(key)
            if previous is not None and previous != cleaned:
                raise MissingVoicePolicyError(
                    f"Missing-voice roles collide after normalization: {previous!r}, {cleaned!r}"
                )
            normalized[key] = cleaned
        canonical = tuple(sorted(normalized.values(), key=str.casefold))
        if self.mode == NARRATOR_ROLES and not canonical:
            raise MissingVoicePolicyError(
                "narrator_roles policy requires at least one exact role"
            )
        if self.mode != NARRATOR_ROLES and canonical:
            raise MissingVoicePolicyError(
                f"{self.mode} policy must not contain an exact role list"
            )
        object.__setattr__(self, "roles", canonical)

    @classmethod
    def from_document(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "mode",
            "roles",
        }:
            raise MissingVoicePolicyError("Missing-voice policy document is malformed")
        if value.get("schema_version") != MISSING_VOICE_POLICY_VERSION:
            raise MissingVoicePolicyError(
                f"Unsupported missing-voice policy version: {value.get('schema_version')!r}"
            )
        roles = value.get("roles")
        if not isinstance(roles, list):
            raise MissingVoicePolicyError("Missing-voice policy roles must be a list")
        return cls(mode=value.get("mode"), roles=tuple(roles))

    def to_document(self):
        return {
            "schema_version": MISSING_VOICE_POLICY_VERSION,
            "mode": self.mode,
            "roles": list(self.roles),
        }

    def applies_to(self, role):
        key = normalize_character_name(str(role or ""))
        if self.mode == NARRATOR_ALL_UNRESOLVED:
            return bool(key) and key != "narrator"
        if self.mode != NARRATOR_ROLES:
            return False
        return any(normalize_character_name(value) == key for value in self.roles)
