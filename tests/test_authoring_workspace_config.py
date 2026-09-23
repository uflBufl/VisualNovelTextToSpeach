import unittest

from vntts.authoring.workspace_config import (
    workspace_config_fingerprint,
    workspace_id_for_config,
    workspace_successor_config_fingerprint,
)


class WorkspaceSuccessorConfigTest(unittest.TestCase):
    def test_successor_overlay_matches_explicit_fingerprint(self):
        workspace = {
            "story_index": {"path": "inputs/story-index.jsonl"},
            "voice_manifest": {"path": "inputs/voice/manifest.json"},
            "narrator_character": "Narrator",
            "run_config": {"backend": "pocket-tts"},
            "carry_forward": {"source": "previous"},
            "audio_event_composition": {"old": True},
            "queue_extension": {"queue_sha256": "a" * 64},
        }
        rebase = {"schema": "rebase"}

        actual = workspace_successor_config_fingerprint(
            workspace,
            "legacy-0123456789abcdef01234567",
            overlays={
                "config_rebase": rebase,
                "audio_event_composition": None,
                "run_config": {"backend": "moss-tts"},
            },
        )

        expected = workspace_config_fingerprint(
            "legacy-0123456789abcdef01234567",
            workspace["story_index"],
            workspace["voice_manifest"],
            "Narrator",
            {"backend": "moss-tts"},
            workspace["carry_forward"],
            config_rebase=rebase,
            queue_extension=workspace["queue_extension"],
        )
        self.assertEqual(actual, expected)
        self.assertEqual(
            workspace_id_for_config("legacy-0123456789abcdef01234567", actual),
            f"resume-0123456789abcdef01234567-{actual[:16]}",
        )

    def test_successor_rejects_unknown_overlay(self):
        with self.assertRaisesRegex(ValueError, "Unknown workspace successor overlays"):
            workspace_successor_config_fingerprint(
                {}, "legacy-0123456789abcdef01234567", overlays={"unknown": 1}
            )
