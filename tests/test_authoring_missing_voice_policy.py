import unittest

from vntts.authoring.missing_voice_policy import (
    BLOCK_MISSING_VOICE,
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)


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
