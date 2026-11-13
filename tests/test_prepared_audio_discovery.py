import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.generated_audio import GeneratedAudioIndex

from tests.story_fixtures import write_synthetic_game_pack
from vntts.current_voice import CurrentVoiceSnapshot
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.prepared_audio_discovery import (
    PreparedAudioSnapshot,
    discover_prepared_audio,
)
from vntts.settings import AppSettings


class PreparedAudioDiscoveryTests(unittest.TestCase):
    def test_archived_tts_excludes_only_validated_full_original_audio(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            for completeness, authoritative, speaker, status, eligible in (
                ("full", True, "Ada", "available", False),
                ("partial", True, "Ada", "available", True),
                ("unknown", False, "Ada", "available", True),
                ("unknown", False, "Narrator", "absent", True),
            ):
                with self.subTest(completeness=completeness, speaker=speaker):
                    line = replace(
                        source.preloader.dialogue[0],
                        speaker=speaker,
                        source_audio_status=status,
                        source_audio_completeness=completeness,
                        source_audio_authoritative=authoritative,
                        source_audio_duration_seconds=1.0,
                    )
                    snapshot = PreparedAudioSnapshot(
                        (replace(source, preloader=type(source.preloader)((line,))),)
                    )
                    with patch.object(
                        source.library,
                        "find_with_preflight",
                        wraps=source.library.find_with_preflight,
                    ) as preflight:
                        library = snapshot.library_for(lambda entry: True)
                        self.assertEqual(
                            library.has_entry(line_id, text_hash), eligible
                        )
                        self.assertEqual(bool(library.index.entries), eligible)
                        self.assertEqual(
                            library.find(line_id, text_hash) is not None, eligible
                        )
                    self.assertEqual(preflight.called, eligible)
                    self.assertIs(snapshot.preloader.line_for_id(line_id), line)

    def test_full_original_authority_survives_older_archive_metadata(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            old = discover_prepared_audio(game_pack=pack).sources[0]
            line = replace(
                old.preloader.dialogue[0],
                source_audio_status="available",
                source_audio_completeness="full",
                source_audio_authoritative=True,
                source_audio_duration_seconds=1.0,
            )
            current = replace(
                old,
                content_sha256="f" * 64,
                preloader=type(old.preloader)((line,)),
                library=GeneratedAudioLibrary(
                    GeneratedAudioIndex(old.library.manifest_path, {}, [])
                ),
            )
            for sources in ((old, current), (current, old)):
                with self.subTest(old_first=sources[0] is old):
                    snapshot = PreparedAudioSnapshot(sources)
                    self.assertIs(snapshot.preloader.line_for_id(line_id), line)
                    library = snapshot.library_for(lambda entry: True)
                    self.assertFalse(library.has_entry(line_id, text_hash))
                    self.assertIsNone(library.find(line_id, text_hash))

    def test_current_voice_matching_receives_original_reference_identity(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            voices = Mock(spec=CurrentVoiceSnapshot)
            voices.matches.return_value = True
            references = Mock()
            references.for_entry.return_value = ("a" * 64,)
            snapshot = PreparedAudioSnapshot(
                (replace(snapshot.sources[0], reference_sources=references),)
            )
            with patch(
                "vntts.prepared_audio_discovery.RecordedReferenceSources.capture",
                return_value=references,
            ):
                library = snapshot.library_for(voices)
            self.assertTrue(library.has_entry(line_id, text_hash))
            self.assertEqual(voices.matches.call_args.kwargs["role"], "Ada")
            self.assertEqual(
                voices.matches.call_args.kwargs["reference_source_sha256s"], ("a" * 64,)
            )
            for role, expected in (
                ("Canonical", "Canonical"),
                ("Canonical__vntts_123456abcdef", "Ada"),
            ):
                source = replace(snapshot.sources[0], voice_roles={line_id: role})
                with patch(
                    "vntts.prepared_audio_discovery.RecordedReferenceSources.capture",
                    return_value=references,
                ):
                    library = PreparedAudioSnapshot((source,)).library_for(voices)
                self.assertTrue(library.has_entry(line_id, text_hash))
                self.assertEqual(voices.matches.call_args.kwargs["role"], expected)

    def test_catalog_packs_and_new_progress_are_discovered_without_full_import(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(assets)
            bindings = json.loads(pack.read_text())["components"]
            story = assets / bindings["story_index"]["path"]
            content = inspect_story_index(story, game_id="synthetic-game")
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(
                content, [selection.selection_id for selection in content.selections]
            )
            published = (
                store.directory_for(job.job_id) / "game-packs" / ("pack-" + "a" * 24)
            )
            shutil.copytree(assets, published)
            identity = "b" * 64
            directory = store.directory_for(job.job_id) / (
                "generation-input-" + identity[:16]
            )
            directory.mkdir()
            shutil.copyfile(story, directory / "story-index.jsonl")
            shutil.copyfile(
                assets / bindings["voice_manifest"]["path"],
                directory / "voice-manifest.json",
            )
            store.catalog.save_input(
                job.job_id,
                {
                    "identity": identity,
                    "job_id": job.job_id,
                    "queue_sha256": "c" * 64,
                    "story_index_sha256": bindings["story_index"]["sha256"],
                    "voice_manifest_sha256": bindings["voice_manifest"]["sha256"],
                },
                directory,
            )
            snapshot = discover_prepared_audio(store)
            self.assertEqual(len(snapshot.sources), 1)
            self.assertFalse(snapshot.changed())
            output = directory.parent / ("generation-output-" + identity[:16])
            output.mkdir()
            progress = json.loads((assets / "generated-audio.json").read_text())
            progress["source_queue_sha256"] = "c" * 64
            progress["vntts.runtime.progress"] = True
            (output / "live-progress-manifest.json").write_text(json.dumps(progress))
            self.assertTrue(snapshot.changed())
            refreshed = discover_prepared_audio(store)
            self.assertEqual(len(refreshed.sources), 2)
            self.assertEqual(
                {source.scope for source in refreshed.sources},
                {snapshot.sources[0].scope},
            )
            self.assertTrue(
                refreshed.library_for(lambda entry: True).has_entry(line_id, text_hash)
            )
            progress_path = output / "live-progress-manifest.json"
            progress_path.write_text(progress_path.read_text() + "\n")
            with patch(
                "vntts.prepared_audio_discovery.read_regular_bytes",
                side_effect=AssertionError("unchanged story reread"),
            ):
                incremental = discover_prepared_audio(store, previous=refreshed)
            old_published = next(
                source
                for source in refreshed.sources
                if source.pack_manifest is not None
            )
            new_published = next(
                source
                for source in incremental.sources
                if source.pack_manifest is not None
            )
            old_progress = next(
                source for source in refreshed.sources if source.pack_manifest is None
            )
            new_progress = next(
                source for source in incremental.sources if source.pack_manifest is None
            )
            self.assertIs(new_published, old_published)
            self.assertIs(new_progress.preloader, old_progress.preloader)
            self.assertIs(
                new_progress.reference_sources, old_progress.reference_sources
            )

    def test_capture_title_binds_unique_game_and_no_title_fails_closed(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            self.assertFalse(snapshot.for_settings(AppSettings()).sources)
            self.assertFalse(
                snapshot.for_settings(
                    AppSettings(game_window_title="Other Game")
                ).sources
            )
            selected = snapshot.for_settings(
                AppSettings(game_window_title="Synthetic Game - Player")
            )
            self.assertIsNotNone(selected.preloader.line_for_id(line_id))
            second_game = replace(
                snapshot.sources[0], game_id="unrelated", game_name="Synthetic Game"
            )
            ambiguous = PreparedAudioSnapshot((*snapshot.sources, second_game))
            self.assertFalse(
                ambiguous.for_settings(
                    AppSettings(game_window_title="Synthetic Game")
                ).sources
            )

    def test_explicit_pack_binding_does_not_use_another_games_text(self):
        with TemporaryDirectory() as temporary:
            pack, _line_id, _text, _text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            self.assertEqual(
                snapshot.for_settings(AppSettings(game_pack=str(pack))).sources,
                snapshot.sources,
            )
            self.assertFalse(
                snapshot.for_settings(
                    AppSettings(
                        game_pack=str(pack.with_name("another-pack.json")),
                        game_window_title="Synthetic Game",
                    )
                ).sources
            )

    def test_known_metadata_change_invalidates_snapshot(self):
        with TemporaryDirectory() as temporary:
            pack, _line_id, _text, _text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            self.assertFalse(snapshot.changed())
            pack.write_text(pack.read_text() + "\n")
            self.assertTrue(snapshot.changed())

    def test_portable_pack_discovery_never_audits_wavs(self):
        with TemporaryDirectory() as temporary:
            original = Path(temporary) / "original"
            original.mkdir()
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(original)
            moved = Path(temporary) / "moved"
            shutil.move(original, moved)

            def read_metadata(path):
                self.assertNotEqual(Path(path).suffix, ".wav")
                return read_regular_bytes(path)

            with (
                patch("vntts.game_pack.import_game_pack", side_effect=AssertionError),
                patch(
                    "vntts.prepared_audio_discovery.read_regular_bytes",
                    side_effect=read_metadata,
                ),
            ):
                snapshot = discover_prepared_audio(game_pack=moved / pack.name)
            self.assertEqual(len(snapshot.sources), 1)
            self.assertEqual(snapshot.sources[0].game_id, "synthetic-game")
            self.assertIsNotNone(snapshot.preloader.line_for_id(line_id))
            library = snapshot.library_for(lambda entry: True)
            self.assertIsNotNone(library.find(line_id, text_hash))

    def test_configured_story_and_manifest_work_without_job_catalog(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(root)
            document = json.loads(pack.read_text())
            snapshot = discover_prepared_audio(
                story_index=root / document["components"]["story_index"]["path"],
                generated_audio_manifest=root / "generated-audio.json",
            )
            self.assertIsNotNone(
                snapshot.library_for(lambda entry: True).find(line_id, text_hash)
            )

    def test_incompatible_archive_voice_is_not_played(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(root)
            snapshot = discover_prepared_audio(game_pack=pack)
            library = snapshot.library_for(lambda entry: False)
            with patch.object(
                GeneratedAudioLibrary, "find_with_preflight", side_effect=AssertionError
            ):
                self.assertFalse(library.has_entry(line_id, text_hash))
                self.assertIsNone(library.find(line_id, text_hash))
            self.assertFalse(library.runtime_progress)

    def test_colliding_games_require_explicit_scope(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(root)
            source = discover_prepared_audio(game_pack=pack).sources[0]
            for other in (replace(source, game_id="different-game"),):
                with self.subTest(scope=other.scope):
                    snapshot = PreparedAudioSnapshot((source, other))
                    self.assertIsNone(snapshot.preloader.line_for_id(line_id))
                    self.assertIsNone(
                        snapshot.library_for(lambda entry: True).find(
                            line_id, text_hash
                        )
                    )
                    scoped = snapshot.library_for(
                        lambda entry: True,
                        game_id=source.game_id,
                        content_sha256=source.content_sha256,
                    )
                    self.assertIsNotNone(scoped.find(line_id, text_hash))

    def test_unchanged_canonical_lines_reuse_audio_across_catalog_revisions(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            newer = replace(source, content_sha256="f" * 64)
            snapshot = PreparedAudioSnapshot((source, newer))
            self.assertEqual(len(snapshot.preloader.dialogue), 1)
            self.assertIsNotNone(
                snapshot.library_for(lambda entry: True).find(line_id, text_hash)
            )
            self.assertEqual(
                len(snapshot.for_settings(AppSettings(game_pack=str(pack))).sources), 2
            )

    def test_conflicting_canonical_speakers_never_share_a_generated_route(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            dialogue = replace(
                source.preloader.dialogue[0], speaker="Different speaker"
            )
            other = replace(source, preloader=type(source.preloader)((dialogue,)))
            snapshot = PreparedAudioSnapshot((source, other))
            self.assertIsNone(snapshot.preloader.line_for_id(line_id))
            self.assertFalse(
                snapshot.library_for(lambda entry: True).has_entry(line_id, text_hash)
            )

    def test_repeated_pack_metadata_deduplicates_canonical_story(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            snapshot = PreparedAudioSnapshot((source, source))
            self.assertEqual(len(snapshot.preloader.dialogue), 1)
            self.assertIsNotNone(snapshot.preloader.line_for_id(line_id))

    def test_changed_component_binding_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack, _line_id, _text, _text_hash, _wav = write_synthetic_game_pack(root)
            (root / "generated-audio.json").write_text("{}")
            warnings = []
            snapshot = discover_prepared_audio(game_pack=pack, warn=warnings.append)
            self.assertFalse(snapshot.sources)
            self.assertTrue(warnings)

    def test_escaping_component_binding_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack, _line_id, _text, _text_hash, _wav = write_synthetic_game_pack(root)
            document = json.loads(pack.read_text())
            document["components"]["story_index"]["path"] = "../story.jsonl"
            pack.write_text(json.dumps(document))
            self.assertFalse(discover_prepared_audio(game_pack=pack).sources)


if __name__ == "__main__":
    unittest.main()
