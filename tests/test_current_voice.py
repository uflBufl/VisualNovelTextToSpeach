import hashlib
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.controller_components import VoiceAssignmentComponent
from vntts.current_voice import CurrentVoiceSnapshot, RecordedReferenceSources
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


def recording(
    role: str, references: tuple[str, ...], *, speaker: str = "donor"
) -> dict:
    entry = {
        "speaker": role,
        "requested_voice_character": role,
        "voice_character": role,
        "provider": "pocket-tts",
        "model": "pocket-tts",
        "generation_profile": "default",
        "audio_sha256": "a" * 64,
        "synthesis_provenance_sha256": "b" * 64,
    }
    entry["vntts.recorded_voice"] = {
        "schema_version": 1,
        "source_character": "Historical donor",
        "speaker": speaker,
        "reference_sha256s": list(references),
        **{
            name: entry[name]
            for name in (
                "voice_character",
                "provider",
                "model",
                "audio_sha256",
                "synthesis_provenance_sha256",
            )
        },
    }
    return entry


class CurrentVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = VoiceLibrary(self.root / "library")
        self.settings = AppSettings(speech_backend="pocket-tts")

    def bind(self, role: str, frames: bytes, *, variant_key: str | None = None):
        path = self.root / f"{hashlib.sha256(frames).hexdigest()}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(frames)
        alternative = self.library.discover(role, path, variant_key=variant_key)
        self.library.select(
            role,
            variant_key=variant_key,
            route="voice",
            source_sha256s=(alternative.sha256,),
        )
        return alternative.sha256

    def test_current_binding_excludes_old_voice_and_can_reuse_it_after_return(self):
        old = self.bind("Ada", b"\0\0" * 10)
        first = CurrentVoiceSnapshot.capture(self.library, self.settings)
        entry = recording("Ada", (old,))
        self.assertTrue(first.matches(entry))
        new = self.bind("Ada", b"\1\0" * 10)
        changed = CurrentVoiceSnapshot.capture(self.library, self.settings, first)
        self.assertIsNot(changed, first)
        self.assertTrue(first.matches(entry))
        self.assertFalse(changed.matches(entry))
        self.assertTrue(changed.matches(recording("Ada", (new,))))
        self.library.select("Ada", route="voice", source_sha256s=(old,))
        returned = CurrentVoiceSnapshot.capture(self.library, self.settings, changed)
        self.assertTrue(returned.matches(entry))

    def test_narrator_change_preserves_independent_roles(self):
        ada = self.bind("Ada", b"\0\0" * 10)
        narrator = self.bind("Narrator", b"\1\0" * 10)
        self.library.select("Unknown", route="narrator")
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        narrated = recording("Unknown", (narrator,))
        self.assertTrue(snapshot.matches(narrated))
        self.bind("Narrator", b"\2\0" * 10)
        changed = CurrentVoiceSnapshot.capture(self.library, self.settings, snapshot)
        self.assertTrue(changed.matches(recording("Ada", (ada,))))
        self.assertFalse(changed.matches(narrated))

    def test_variant_lookup_uses_only_current_family_choice(self):
        old = self.bind("Ada", b"\0\0" * 10, variant_key="old portrait")
        new = self.bind("Ada", b"\1\0" * 10, variant_key="new portrait")
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        self.assertFalse(
            snapshot.matches(recording("Ada", (old,)), variant_key="old portrait")
        )
        self.assertTrue(
            snapshot.matches(recording("Ada", (new,)), variant_key="old portrait")
        )

    def test_cleared_and_live_fallback_choices_do_not_use_archives(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        entry = recording("Ada", (reference,))
        self.library.select("Ada", route="live-fallback")
        self.assertFalse(
            CurrentVoiceSnapshot.capture(self.library, self.settings).matches(entry)
        )
        self.library.clear("Ada")
        self.assertFalse(
            CurrentVoiceSnapshot.capture(self.library, self.settings).matches(entry)
        )

    def test_unverifiable_metadata_never_matches(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        for field in (
            "vntts.recorded_voice",
            "audio_sha256",
            "synthesis_provenance_sha256",
        ):
            with self.subTest(field=field):
                entry = recording("Ada", (reference,))
                del entry[field]
                self.assertFalse(snapshot.matches(entry))

    def test_snapshot_reuse_caches_bindings_without_reading_recording_or_reference(
        self,
    ):
        reference = self.bind("Ada", b"\0\0" * 10)
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        self.assertIs(
            CurrentVoiceSnapshot.capture(self.library, self.settings, snapshot),
            snapshot,
        )
        with (
            patch.object(
                snapshot.library, "binding", wraps=snapshot.library.binding
            ) as binding,
            patch.object(
                snapshot.library,
                "reference_paths",
                side_effect=AssertionError("reference read"),
            ),
        ):
            self.assertTrue(snapshot.matches(recording("Ada", (reference,))))
            self.assertTrue(snapshot.matches(recording("Ada", (reference,))))
        binding.assert_called_once_with("Ada", variant_key=None)

    def test_first_reference_is_the_effective_cloning_identity(self):
        first = self.bind("Ada", b"\0\0" * 10)
        second = self.bind("Ada", b"\1\0" * 10)
        self.library.select("Ada", route="voice", source_sha256s=(first, second))
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        self.assertTrue(snapshot.matches(recording("Ada", (first,))))
        self.assertFalse(snapshot.matches(recording("Ada", (second,))))

    def test_preset_and_narrator_settings_have_scoped_identity(self):
        self.library.select("Ada", route="voice", source_id="preset:alba")
        self.library.select("Unknown", route="narrator")
        first = CurrentVoiceSnapshot.capture(self.library, self.settings)
        changed = CurrentVoiceSnapshot.capture(
            self.library, self.settings.updated(narrator_speaker="marius"), first
        )
        self.assertTrue(changed.matches(recording("Ada", (), speaker="alba")))
        self.assertFalse(changed.matches(recording("Unknown", (), speaker="alba")))
        self.assertTrue(changed.matches(recording("Unknown", (), speaker="marius")))

    def test_explicit_story_role_resolves_synthetic_archive_voice(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        entry = recording("Ada__vntts_old", (reference,))
        self.assertFalse(snapshot.matches(entry))
        self.assertTrue(snapshot.matches(entry, role="Ada"))

    def test_unknown_speaker_uses_current_narrator(self):
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        entry = recording("???", (), speaker="alba")
        entry["requested_voice_character"] = "Old donor"
        self.assertTrue(snapshot.matches(entry))

    def test_unbound_named_role_uses_current_narrator_without_old_voice_fallback(self):
        narrator = self.bind("Narrator", b"\0\0" * 10)
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        self.assertTrue(snapshot.matches(recording("Unbound role", (narrator,))))
        self.assertFalse(snapshot.matches(recording("Unbound role", ("c" * 64,))))

    def test_runtime_refresh_updates_changed_and_removed_current_voices(self):
        registry = CharacterVoiceRegistry([CharacterVoice("Ada", "old-implicit")])
        registry.set_assignment("Ada", "preset:alba")
        registry.set_assignment("Removed alias", "preset:alba")
        controller = SimpleNamespace(
            voice_router=SimpleNamespace(
                registry=registry,
                audio_cache={"old": object()},
                voice_states={"old": object()},
                prompt_audio_codes={"old": object()},
                conditionals={"old": object()},
                clear_runtime_cache=Mock(
                    side_effect=AssertionError("Worker terminated")
                ),
            ),
            _apply_narrator_voice=Mock(),
            _clear_voice_runtime_cache=Mock(),
        )
        self.library.select("Ada", route="voice", source_id="preset:marius")
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        component = VoiceAssignmentComponent(controller)
        snapshot = self.library.read_snapshot()
        projection = component.project_current_voices(snapshot)
        self.assertEqual(registry.resolve("Ada").speaker, "alba")
        with patch(
            "vntts.controller_components.registry_with_voice_library",
            side_effect=AssertionError("Projection must happen before commit"),
        ):
            component.refresh_current_voices(snapshot, projection=projection)
        self.assertEqual(registry.resolve("Ada").speaker, "marius")
        self.assertIsNone(registry.resolve("Removed alias"))
        self.assertEqual(
            controller._apply_narrator_voice.call_args.args[0].speaker, "alba"
        )
        self.library.clear("Ada")
        self.library.clear("Narrator")
        component.refresh_current_voices(self.library.read_snapshot())
        self.assertIsNone(registry.resolve("Ada"))
        controller._apply_narrator_voice.assert_called_with(None)
        controller._clear_voice_runtime_cache.assert_not_called()
        controller.voice_router.clear_runtime_cache.assert_not_called()
        for name in (
            "audio_cache",
            "voice_states",
            "prompt_audio_codes",
            "conditionals",
        ):
            self.assertEqual(getattr(controller.voice_router, name), {})

    def test_generation_engine_model_and_profile_must_match_current_controls(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        entry = recording("Ada", (reference,))
        for changes in (
            {"offline_speech_backend": "moss-tts"},
            {
                "offline_speech_backend": "coqui-xtts",
                "offline_tts_profile": "natural",
            },
        ):
            with self.subTest(changes=changes):
                snapshot = CurrentVoiceSnapshot.capture(
                    self.library, self.settings.updated(**changes)
                )
                self.assertFalse(snapshot.matches(entry))
        settings = self.settings.updated(
            offline_speech_backend="coqui-xtts", offline_tts_model="chosen-model"
        )
        entry["provider"] = "coqui-xtts"
        entry["generation_profile"] = settings.offline_tts_profile
        entry["vntts.recorded_voice"]["provider"] = "coqui-xtts"
        snapshot = CurrentVoiceSnapshot.capture(self.library, settings)
        self.assertFalse(snapshot.matches(entry))
        entry["model"] = "chosen-model"
        entry["vntts.recorded_voice"]["model"] = "chosen-model"
        self.assertTrue(snapshot.matches(entry))
        entry["generation_profile"] = "natural"
        self.assertFalse(snapshot.matches(entry))

    def test_offline_controls_are_independent_of_live_engine_preferences(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        settings = self.settings.updated(
            speech_backend="coqui-xtts", tts_model="live-model", tts_profile="natural"
        )
        snapshot = CurrentVoiceSnapshot.capture(self.library, settings)
        self.assertTrue(snapshot.matches(recording("Ada", (reference,))))

    def test_verified_manifest_maps_normalized_recording_back_to_raw_source(self):
        reference = self.bind("Ada", b"\0\0" * 10)
        path = self.root / "voice-manifest.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Ada",
                            "speaker": "donor",
                            "vntts.source_character": "Historical donor",
                            "references": [f"references/{reference}.wav"],
                        }
                    ],
                }
            )
        )
        sources = RecordedReferenceSources.capture(
            path, hashlib.sha256(path.read_bytes()).hexdigest()
        )
        entry = recording("Ada", ("c" * 64,))
        snapshot = CurrentVoiceSnapshot.capture(self.library, self.settings)
        self.assertFalse(snapshot.matches(entry))
        self.assertTrue(
            snapshot.matches(entry, reference_source_sha256s=sources.for_entry(entry))
        )
        entry["vntts.recorded_voice"]["source_character"] = "Other donor"
        self.assertIsNone(sources.for_entry(entry))

    def test_changed_manifest_and_unrecognized_reference_names_are_not_trusted(self):
        path = self.root / "voice-manifest.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Ada",
                            "speaker": "donor",
                            "references": ["references/unknown.wav"],
                        }
                    ],
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            RecordedReferenceSources.capture(path, "d" * 64)
        sources = RecordedReferenceSources.capture(
            path, hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertIsNone(sources.for_entry(recording("Ada", ("c" * 64,))))


if __name__ == "__main__":
    unittest.main()
