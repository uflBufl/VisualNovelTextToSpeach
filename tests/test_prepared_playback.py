import json
import shutil
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.pregeneration_fixtures import fixture
from tests.test_pregeneration_activation import published_pack
from vntts.pregeneration_activation import (
    OfflinePackActivationError,
    OfflinePackActivator,
)
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.prepared_playback import PreparedPlaybackStore
from vntts.prepared_sequence_status import (
    SequenceUnavailableReason,
    load_sequence_unavailable_reason,
)
from vntts.profiles import GameProfile, GameProfileStore
from vntts.settings import AppSettings, load_app_settings
from vntts.versioned_json import write_versioned_json


class PreparedPlaybackTest(unittest.TestCase):
    def test_sequence_reason_survives_activation_and_restore_only_for_selected_profile(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, inputs, generated, _items = fixture(root / "generated")
            reason = SequenceUnavailableReason.MANUAL_BOUNDARY
            pack = OfflinePackPublisher().publish(
                job, inputs, generated, sequence_unavailable_reason=reason
            )
            preferences = AppSettings(
                active_profile_id="game-a", output_volume_percent=37
            )
            settings_path = preferences.save(root / "settings.json")
            profile = GameProfile.from_settings(
                "Game", preferences, profile_id=preferences.active_profile_id
            )
            profiles_path = GameProfileStore(root / "profiles.json", (profile,)).save()
            original_settings = settings_path.read_bytes()
            original_profiles = profiles_path.read_bytes()
            store = PreparedPlaybackStore(root / "playback.json")
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            activated = OfflinePackActivator(save_state=store.save).activate(
                preferences, pack, controller
            )
            self.assertIsNone(activated.settings.live_sequence_plan)
            self.assertEqual(activated.settings.live_sequence_mode, "off")
            self.assertEqual(
                load_sequence_unavailable_reason(activated.settings.game_pack), reason
            )
            restarted = PreparedPlaybackStore(store.path).restore(
                load_app_settings(settings_path, environment={})
            )
            self.assertEqual(restarted.game_pack, str(pack.manifest))
            self.assertEqual(
                load_sequence_unavailable_reason(restarted.game_pack), reason
            )
            other_profile = preferences.updated(active_profile_id="game-b")
            unrelated = PreparedPlaybackStore(store.path).restore(other_profile)
            self.assertIs(unrelated, other_profile)
            self.assertIsNone(unrelated.game_pack)
            self.assertIsNone(load_sequence_unavailable_reason(unrelated.game_pack))
            self.assertEqual(settings_path.read_bytes(), original_settings)
            self.assertEqual(profiles_path.read_bytes(), original_profiles)

    def test_activation_and_restart_preserve_preferences_and_profile(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = published_pack(root / "generated")
            preferences = AppSettings(
                output_volume_percent=37,
                speech_rate_percent=135,
                force_live_narrator=True,
                active_profile_id="game-a",
            )
            path = preferences.save(root / "settings.json")
            original = path.read_bytes()
            store = PreparedPlaybackStore(root / "local" / "playback.json")
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            result = OfflinePackActivator(save_state=store.save).activate(
                preferences, pack, controller
            )
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(result.state_path, store.path)
            self.assertEqual(result.settings.game_pack, str(pack.manifest))
            self.assertEqual(result.settings.audio_source_policy, "prefer-generated")
            self.assertEqual(result.settings.output_volume_percent, 37)
            self.assertEqual(result.settings.speech_rate_percent, 135)
            self.assertFalse(result.settings.force_live_narrator)
            self.assertTrue(preferences.force_live_narrator)
            saved = load_app_settings(path, environment={})
            self.assertEqual(saved, preferences)
            restarted = PreparedPlaybackStore(store.path).restore(saved)
            self.assertEqual(restarted, result.settings)

    def test_missing_generated_audio_or_state_keeps_preferences(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = published_pack(root / "generated")
            preferences = AppSettings(output_volume_percent=37)
            store = PreparedPlaybackStore(root / "local" / "playback.json")
            store.save(preferences, pack)
            original = store.path.read_bytes()
            shutil.rmtree(pack.directory)
            with self.assertLogs("vntts.prepared_playback", level="ERROR"):
                self.assertEqual(store.restore(preferences), preferences)
            self.assertEqual(store.path.read_bytes(), original)
            store.path.unlink()
            self.assertEqual(store.restore(preferences), preferences)

    def test_profile_and_source_changes_do_not_reuse_another_selection(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = published_pack(root / "generated")
            preferences = AppSettings(active_profile_id="a")
            store = PreparedPlaybackStore(root / "playback.json")
            store.save(preferences, pack)
            other = preferences.updated(active_profile_id="b")
            self.assertEqual(store.restore(other), other)
            other_source = preferences.updated(story_index="another-story.json")
            self.assertEqual(store.restore(other_source), other_source)
            changed_volume = preferences.updated(output_volume_percent=42)
            restored = store.restore(changed_volume)
            self.assertEqual(restored.output_volume_percent, 42)
            self.assertEqual(restored.game_pack, str(pack.manifest))
            changed_routing = preferences.updated(
                audio_source_policy="prefer-game-audio",
                force_live_narrator=True,
                live_sequence_mode="audio-manual",
            )
            routed = store.restore(changed_routing)
            self.assertEqual(routed.game_pack, str(pack.manifest))
            self.assertEqual(routed.audio_source_policy, "prefer-game-audio")
            self.assertTrue(routed.force_live_narrator)
            self.assertEqual(routed.live_sequence_mode, "audio-manual")
            other_pack = published_pack(root / "other-generated")
            store.save(other, other_pack)
            self.assertEqual(store.restore(preferences).game_pack, str(pack.manifest))
            self.assertEqual(store.restore(other).game_pack, str(other_pack.manifest))

    def test_corrupt_or_changed_state_is_logged_without_changing_preferences(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = published_pack(root / "generated")
            store = PreparedPlaybackStore(root / "playback.json")
            preferences = AppSettings()
            store.save(preferences, pack)
            document = json.loads(store.path.read_bytes())
            document["profiles"][""]["identity"] = "b" * 64
            for raw in (b"{broken", json.dumps(document).encode()):
                store.path.write_bytes(raw)
                with self.assertLogs("vntts.prepared_playback", level="ERROR"):
                    self.assertEqual(store.restore(preferences), preferences)
                self.assertEqual(store.path.read_bytes(), raw)

    def test_failed_state_save_restores_actual_previous_runtime(self):
        with TemporaryDirectory() as temporary:
            pack = published_pack(Path(temporary) / "generated")
            preferences = AppSettings(output_volume_percent=37)
            previous_runtime = preferences.updated(game_pack="previous.json")
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            save = Mock(side_effect=OSError("disk full"))
            with self.assertRaisesRegex(OfflinePackActivationError, "disk full"):
                OfflinePackActivator(save_state=save).activate(
                    preferences,
                    pack,
                    controller,
                    previous_runtime_settings=previous_runtime,
                )
            save.assert_called_once_with(preferences, pack)
            self.assertEqual(
                controller.apply_settings.call_args.args, (previous_runtime,)
            )

    def test_internal_stale_paths_are_ignored_without_resetting_user_settings(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal = root / "local" / "pregeneration" / "deleted"
            preferences = AppSettings(
                output_volume_percent=37,
                read_hotkey="<ctrl>+<alt>+r",
                game_pack=str(internal / "game-pack.json"),
                story_index=str(internal / "story.json"),
                voice_manifest=str(internal / "voices.json"),
                generated_audio_manifest=str(internal / "audio.json"),
            )
            with patch(
                "vntts.settings.get_local_data_directory", return_value=root / "local"
            ):
                path = root / "settings.json"
                write_versioned_json(
                    path, preferences.schema_version, asdict(preferences)
                )
                before = path.read_bytes()
                with patch(
                    "vntts.game_pack.import_game_pack",
                    side_effect=AssertionError("stale pack read"),
                ):
                    loaded = load_app_settings(path, environment={})
                self.assertEqual(loaded.output_volume_percent, 37)
                self.assertEqual(loaded.read_hotkey, preferences.read_hotkey)
                self.assertIsNone(loaded.game_pack)
                self.assertIsNone(loaded.story_index)
                self.assertIsNone(loaded.generated_audio_manifest)
                self.assertEqual(path.read_bytes(), before)
                loaded.save(path)
                self.assertEqual(
                    json.loads(path.read_bytes())["output_volume_percent"], 37
                )
                profile = GameProfile.from_settings("Game", preferences)
                self.assertIsNone(profile.game_pack)
                profile_values = profile.to_mapping()
                profile_values["game_pack"] = str(internal / "game-pack.json")
                self.assertIsNone(GameProfile.from_mapping(profile_values).game_pack)
