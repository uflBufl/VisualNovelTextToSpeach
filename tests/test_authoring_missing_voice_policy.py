import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)

from vntts.authoring.cli_generation import _synthesis_character_overrides
from vntts.authoring.missing_voice_policy import (
    BLOCK_MISSING_VOICE,
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.workspace_creation import _narrator_fallback_overrides
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class MissingVoicePolicyTest(unittest.TestCase):
    def test_default_is_explicit_block(self):
        policy = MissingVoicePolicy.from_document(None)

        self.assertEqual(policy.mode, BLOCK_MISSING_VOICE)
        self.assertFalse(policy.applies_to("Hotelier"))
        self.assertEqual(
            policy.to_document(),
            {"schema_version": 1, "mode": "block", "roles": []},
        )

    def test_exact_role_policy_is_normalized_but_not_broadened(self):
        policy = MissingVoicePolicy(NARRATOR_ROLES, ("Poacher II", "Poacher I"))

        self.assertTrue(policy.applies_to("poacher i"))
        self.assertTrue(policy.applies_to("Poacher II"))
        self.assertFalse(policy.applies_to("Poacher"))
        self.assertFalse(policy.applies_to("Narrator"))

    def test_all_unresolved_excludes_narrator_itself(self):
        policy = MissingVoicePolicy(NARRATOR_ALL_UNRESOLVED)

        self.assertTrue(policy.applies_to("Glyndŵr"))
        self.assertFalse(policy.applies_to("Narrator"))

    def test_fallback_checks_distinct_roles_in_order_and_refreshes_each_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            present = root / "present.wav"
            missing = root / "missing.wav"
            narrator_path = root / "narrator.wav"
            narrator = CharacterVoice("Narrator", "narrator", reference=narrator_path)
            registry = CharacterVoiceRegistry(
                (
                    narrator,
                    CharacterVoice(
                        "Present", "present", reference=present, aliases=("Alias",)
                    ),
                    CharacterVoice("Missing", "missing", reference=missing),
                    CharacterVoice("Empty", "empty"),
                )
            )
            policy = MissingVoicePolicy(
                NARRATOR_ROLES, ("Missing", "Unknown", "Alias", "Present")
            )
            requested = ("Narrator", "Missing", "Alias", "Empty", "Unknown", "Present")
            real_is_file = Path.is_file
            for workspace in (False, True):
                for repeats in (1, 100):
                    with self.subTest(workspace=workspace, repeats=repeats):
                        queue = VoiceGenerationQueue(
                            root / "queue.jsonl",
                            {},
                            tuple(
                                VoiceGenerationQueueItem(
                                    queue_id=str(index),
                                    line_id=str(index),
                                    text_sha256="a" * 64,
                                    text="Text",
                                    action="synthesize",
                                    speaker="???" if role == "Narrator" else "Speaker",
                                    voice_character="Present"
                                    if role == "Narrator"
                                    else role,
                                    source_audio_status=None,
                                    source_audio_reason=None,
                                    state="pending",
                                    document={},
                                )
                                for index, role in enumerate(requested * repeats)
                            ),
                        )
                        present.write_bytes(b"reference")
                        narrator_path.write_bytes(b"narrator")
                        missing.unlink(missing_ok=True)
                        for stage in ("initial", "changed", "narrator-restored"):
                            if stage == "changed":
                                missing.write_bytes(b"recovered")
                                present.unlink()
                                narrator_path.unlink()
                            elif stage == "narrator-restored":
                                narrator_path.write_bytes(b"narrator")
                            expected = (
                                ["Missing", "Unknown"]
                                if stage == "initial"
                                else ["Alias", "Unknown", "Present"]
                            )
                            if workspace and stage == "changed":
                                expected = []
                            with (
                                patch.object(
                                    registry, "resolve", wraps=registry.resolve
                                ) as resolve,
                                patch.object(
                                    Path,
                                    "is_file",
                                    autospec=True,
                                    side_effect=real_is_file,
                                ) as reference_checks,
                            ):
                                overrides = (
                                    _narrator_fallback_overrides(
                                        queue, registry, narrator, policy
                                    )
                                    if workspace
                                    else _synthesis_character_overrides(
                                        queue, registry, policy
                                    )
                                )
                            self.assertEqual(list(overrides), expected)
                            self.assertEqual(
                                overrides, dict.fromkeys(expected, "Narrator")
                            )
                            self.assertEqual(
                                [call.args[0] for call in resolve.call_args_list],
                                list(requested),
                            )
                            expected_checks = {present: 2, missing: 1}
                            if workspace:
                                expected_checks[narrator_path] = 2
                            self.assertEqual(
                                Counter(
                                    call.args[0]
                                    for call in reference_checks.call_args_list
                                ),
                                expected_checks,
                            )

    def test_malformed_or_ambiguous_documents_fail_closed(self):
        with self.assertRaises(MissingVoicePolicyError):
            MissingVoicePolicy(NARRATOR_ROLES, ())
        with self.assertRaises(MissingVoicePolicyError):
            MissingVoicePolicy(BLOCK_MISSING_VOICE, ("Hotelier",))
        with self.assertRaises(MissingVoicePolicyError):
            MissingVoicePolicy(NARRATOR_ROLES, ("Mrs. Owen", '"Mrs. Owen"'))
        with self.assertRaises(MissingVoicePolicyError):
            MissingVoicePolicy.from_document(
                {"schema_version": 2, "mode": BLOCK_MISSING_VOICE, "roles": []}
            )


if __name__ == "__main__":
    unittest.main()
