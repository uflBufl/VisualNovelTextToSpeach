import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.game_pack import GamePackError
from vntts_artifacts.story_index import (
    load_story_index,
    load_story_index_document,
)
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.story_fixtures import write_synthetic_game_pack
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_pack import apply_game_pack, import_game_pack, main
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.settings import AppSettings, load_app_settings
from vntts.source_audio_semantics import (
    load_source_audio_semantic_evidence,
)
from vntts.story_index_snapshot import load_story_index_snapshot
from vntts.voices import CharacterVoiceRegistry


def write_saved_voice_catalog(root, *, stale_source_id):
    reference = root / "saved-catalog" / "references" / "narrator.wav"
    write_pcm16_wav(reference, np.zeros(240, dtype=np.float32), 24_000)
    manifest = root / "saved-catalog" / "voice-manifest.json"
    write_voice_manifest(
        manifest,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Game narrator active",
                    "speaker": "narrator-v1",
                    "references": ["references/narrator.wav"],
                }
            ],
            "vntts.game_narrator": {
                "source_id": stale_source_id,
                "character": "Old narrator",
                "base_manifest_sha256": "0" * 64,
            },
        },
    )
    return manifest


class GamePackImportTest(unittest.TestCase):
    def test_import_records_bounded_file_work(self):
        with TemporaryDirectory() as directory:
            pack_path, *_unused = write_synthetic_game_pack(Path(directory))
            with patch("vntts.support.record_background_operation") as record:
                import_game_pack(pack_path)

        operation = record.call_args
        self.assertEqual(operation.args[0], "game-pack-validation")
        self.assertEqual(operation.args[2], "complete")
        self.assertGreater(operation.kwargs["files_examined"], 0)
        self.assertGreater(operation.kwargs["bytes_examined"], 0)

    def test_implicit_reload_uses_pack_catalog_without_voice_precedence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)
            saved = write_saved_voice_catalog(
                root, stale_source_id="character:missing-narrator"
            )
            settings = AppSettings(
                game_pack=str(pack_path),
                voice_manifest=str(saved),
            )

            implicit = apply_game_pack(settings)
            explicit = apply_game_pack(settings, pack_path)

        expected = str((root / "voice-manifest.json").resolve())
        self.assertEqual(implicit.voice_manifest, expected)
        self.assertEqual(explicit.voice_manifest, expected)

    def test_implicit_reload_drops_stale_catalog_without_active_character_source(self):
        for narrator_source in ("preset:alba", "default"):
            with (
                self.subTest(narrator_source=narrator_source),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                pack_path, *_unused = write_synthetic_game_pack(root)
                saved = write_saved_voice_catalog(
                    root, stale_source_id="character:missing-narrator"
                )

                result = apply_game_pack(
                    AppSettings(
                        game_pack=str(pack_path),
                        voice_manifest=str(saved),
                    )
                )

                self.assertEqual(
                    result.voice_manifest,
                    str((root / "voice-manifest.json").resolve()),
                )

    def test_import_preflights_checksum_bound_semantic_evidence_extension(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(
                root,
                include_semantics=True,
            )

            with (
                patch(
                    "vntts_artifacts.game_pack.load_story_index",
                    wraps=load_story_index,
                ) as artifact_story_load,
                patch(
                    "vntts.game_pack.load_story_index_snapshot",
                    wraps=load_story_index_snapshot,
                ) as vntts_story_load,
                patch(
                    "vntts.source_audio_semantics.load_story_index_document",
                    wraps=load_story_index_document,
                ) as redundant_story_load,
            ):
                imported = import_game_pack(pack_path)
                import_game_pack(pack_path)

        self.assertEqual(
            imported.source_audio_semantic_evidence.name,
            "source-audio-semantic-evidence.json",
        )
        self.assertEqual(artifact_story_load.call_count, 2)
        self.assertEqual(vntts_story_load.call_count, 1)
        redundant_story_load.assert_not_called()

    def test_import_rejects_story_replaced_during_semantic_preflight(self):
        for include_semantics in (False, True):
            with (
                self.subTest(include_semantics=include_semantics),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                pack_path, *_unused = write_synthetic_game_pack(
                    root, include_semantics=include_semantics
                )
                story_path = root / "story-index.jsonl"

                def replace_story_before_parse(path, payload):
                    rows = story_path.read_text(encoding="utf-8").splitlines()
                    record = json.loads(rows[1])
                    record["speaker"] = "Changed speaker"
                    rows[1] = json.dumps(record)
                    story_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
                    return load_story_index_snapshot(path, payload)

                with (
                    patch(
                        "vntts.game_pack.load_story_index_snapshot",
                        side_effect=replace_story_before_parse,
                    ),
                    self.assertRaisesRegex(GamePackError, "checksum changed"),
                ):
                    import_game_pack(pack_path)

    def test_import_wraps_missing_files_after_semantic_parse(self):
        for component, include_semantics in (
            ("story", False),
            ("story", True),
            ("evidence", True),
        ):
            with (
                self.subTest(component=component, include_semantics=include_semantics),
                TemporaryDirectory() as directory,
            ):
                pack_path, *_unused = write_synthetic_game_pack(
                    Path(directory), include_semantics=include_semantics
                )
                if component == "story":
                    loader = load_story_index_snapshot
                    target = "vntts.game_pack.load_story_index_snapshot"
                else:
                    loader = load_source_audio_semantic_evidence
                    target = "vntts.game_pack.load_source_audio_semantic_evidence"

                def remove_after_parse(path, *args):
                    document = loader(path, *args)
                    Path(path).unlink()
                    return document

                with (
                    patch(target, side_effect=remove_after_parse),
                    self.assertRaises(GamePackError),
                ):
                    import_game_pack(pack_path)

    def test_import_rejects_modified_semantic_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(
                root,
                include_semantics=True,
            )
            (root / "source-audio-semantic-evidence.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(GamePackError, "checksum changed"):
                import_game_pack(pack_path)

    def test_semantic_evidence_requires_canonical_portable_relative_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(
                root, include_semantics=True
            )
            document = json.loads(pack_path.read_text(encoding="utf-8"))
            extension = document["vntts.authoring"]["source_audio_semantic_evidence"]
            payload = (root / extension["path"]).read_bytes()
            unsafe_paths = (
                "story\\evidence.json",
                "./source-audio-semantic-evidence.json",
                "story//evidence.json",
                "C:evidence.json",
                "../evidence.json",
            )
            for relative in unsafe_paths:
                with self.subTest(path=relative):
                    # Give aliases real bound bytes so a missing file cannot hide
                    # acceptance of an invalid path. Windows forbids drive-like names.
                    if relative not in {"C:evidence.json", "../evidence.json"}:
                        alias = root / relative
                        alias.parent.mkdir(parents=True, exist_ok=True)
                        alias.write_bytes(payload)
                    extension["path"] = relative
                    atomic_write_json(pack_path, document)
                    with self.assertRaisesRegex(
                        GamePackError, "POSIX-relative|stay inside|leaves|unsafe"
                    ):
                        import_game_pack(pack_path)

    def test_import_rejects_boolean_semantic_evidence_entry_count(self):
        with TemporaryDirectory() as directory:
            pack_path, *_unused = write_synthetic_game_pack(
                Path(directory), include_semantics=True
            )
            document = json.loads(pack_path.read_text(encoding="utf-8"))
            document["vntts.authoring"]["source_audio_semantic_evidence"][
                "entry_count"
            ] = True
            atomic_write_json(pack_path, document)

            with self.assertRaisesRegex(GamePackError, "extension changed"):
                import_game_pack(pack_path)

    def test_public_producer_pack_reaches_all_public_vntts_consumers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app_data = root / "app-data"
            app_data.mkdir()
            sentinel = app_data / "keep.txt"
            sentinel.write_text("preserve me", encoding="utf-8")
            pack_path, line_id, text, text_hash, _generated_wav = (
                write_synthetic_game_pack(root)
            )

            imported = import_game_pack(pack_path)
            settings = imported.apply_to(
                AppSettings(
                    screenshot_directory=str(app_data),
                    generated_audio_manifest="stale-generated.json",
                )
            )
            line = ChapterVoicePreloader.load_optional(
                settings.story_index
            ).resolve_exact("Ada", text)
            voice = CharacterVoiceRegistry.from_file(settings.voice_manifest).resolve(
                "Ada"
            )
            generated = GeneratedAudioLibrary.load_optional(
                settings.generated_audio_manifest
            ).find(line_id, text_hash)

            self.assertEqual(imported.pack.game_id, "synthetic-game")
            self.assertEqual(line.line_id, line_id)
            self.assertEqual(voice.speaker, "ada-v1")
            self.assertEqual(generated.sample_rate, 24_000)
            self.assertEqual(settings.game_pack, str(pack_path.resolve()))
            self.assertEqual(settings.screenshot_directory, str(app_data))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me")

    def test_loading_settings_preflights_configured_pack_and_applies_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = load_app_settings(
                root / "missing-settings.json",
                environment={"VNTTS_GAME_PACK": str(pack_path)},
            )

        self.assertEqual(settings.game_pack, str(pack_path.resolve()))
        self.assertEqual(Path(settings.story_index).name, "story-index.jsonl")
        self.assertEqual(Path(settings.voice_manifest).name, "voice-manifest.json")
        self.assertEqual(
            Path(settings.generated_audio_manifest).name,
            "generated-audio.json",
        )

    def test_pack_without_generated_audio_clears_stale_generated_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(
                root, include_generated=False
            )

            settings = apply_game_pack(
                AppSettings(generated_audio_manifest="stale-generated.json"),
                pack_path,
            )

        self.assertIsNone(settings.generated_audio_manifest)

    def test_pack_import_clears_a_sequence_plan_bound_to_another_pack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = apply_game_pack(
                AppSettings(
                    live_sequence_plan="stale-plan.json",
                    live_sequence_mode="shadow",
                ),
                pack_path,
            )

        self.assertIsNone(settings.live_sequence_plan)
        self.assertEqual(settings.live_sequence_mode, "off")

    def test_reapplying_same_pack_preserves_explicit_external_sequence_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = apply_game_pack(
                AppSettings(
                    game_pack=str(pack_path),
                    live_sequence_plan="external-live-sequence.json",
                    live_sequence_mode="audio-auto",
                )
            )

        self.assertEqual(
            settings.live_sequence_plan,
            "external-live-sequence.json",
        )
        self.assertEqual(settings.live_sequence_mode, "audio-auto")

    def test_pack_import_applies_its_checksum_bound_sequence_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root, include_sequence=True)

            settings = apply_game_pack(
                AppSettings(
                    live_sequence_plan="stale-plan.json",
                    live_sequence_mode="audio-manual",
                ),
                pack_path,
            )

        self.assertEqual(Path(settings.live_sequence_plan).name, "live-sequence.json")
        self.assertEqual(settings.live_sequence_mode, "audio-manual")

    def test_preflight_rejects_modified_referenced_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_identity, generated_wav = write_synthetic_game_pack(root)
            generated_wav.write_bytes(b"tampered")

            with self.assertRaisesRegex(GamePackError, "checksum does not match"):
                import_game_pack(pack_path)

    def test_cli_preflight_reports_resolved_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main([str(pack_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["game_id"], "synthetic-game")
        self.assertEqual(payload["game_pack"], str(pack_path.resolve()))

    def test_runtime_pack_import_never_installs_authoring_asr(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)
            with patch(
                "vntts.authoring.asr_model.install_managed_asr_model",
                side_effect=AssertionError("runtime attempted an authoring download"),
            ) as install:
                imported = import_game_pack(pack_path)

        self.assertEqual(imported.pack.game_id, "synthetic-game")
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
