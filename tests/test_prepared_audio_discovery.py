import gc
import json
import shutil
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.generated_audio import GeneratedAudioIndex

from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from tests.story_fixtures import write_synthetic_game_pack
from tests.symlink_support import symlink_or_skip
from vntts import generation_database
from vntts.authoring.bulk_generation import run_bulk_generation
from vntts.content_revision import ContentRevision, source_signature
from vntts.current_voice import CurrentVoiceSnapshot
from vntts.document_identity import sha256_file
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    inspect_story_index,
    open_reading_catalog,
)
from vntts.prepared_audio_discovery import (
    PreparedAudioSnapshot,
    discover_prepared_audio,
)
from vntts.reading_catalog import ReadingCatalog
from vntts.settings import AppSettings
from vntts.story_index_snapshot import load_story_index_snapshot


class PreparedAudioDiscoveryTests(unittest.TestCase):
    @staticmethod
    def revision(path):
        signature = source_signature(path)
        payload = read_regular_bytes(path)
        return ContentRevision.capture(
            path, payload, load_story_index_snapshot(path, payload), signature=signature
        )

    def test_candidate_and_archive_queries_do_not_enumerate_dialogue(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            store = snapshot.sources[0].preloader.store
            with patch.object(
                store, "iter_rows", side_effect=AssertionError("enumerated dialogue")
            ):
                preloader = snapshot.preloader
                self.assertTrue(preloader.has_dialogue)
                line = preloader.line_for_id(line_id)
                self.assertEqual(preloader.resolve_exact(line.speaker, text), line)
                library = snapshot.library_for(lambda _entry: True)
                self.assertTrue(library.has_entry(line_id, text_hash))

                def candidates(*_args, **_kwargs):
                    yield line
                    self.fail("consumed candidates past the first witness")

                with patch.object(store, "iter_candidates", side_effect=candidates):
                    self.assertIs(next(preloader.store.iter_candidates(text)), line)

    def test_admitted_pack_and_catalog_are_reused_without_file_checks(self):
        with TemporaryDirectory() as temporary:
            pack, _line_id, _text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            source = snapshot.sources[0]
            story_index = source.story_index
            snapshot = discover_prepared_audio(game_pack=pack, story_index=story_index)
            for path in (
                pack,
                story_index,
                source.library.manifest_path,
                source.voice_manifest,
            ):
                path.write_bytes(path.read_bytes() + b"\n")
            story_index.with_name("source-audio-semantic-evidence.json").write_text(
                "{}"
            )
            with (
                patch(
                    "vntts.prepared_audio_discovery._signature",
                    side_effect=AssertionError("admitted file polled"),
                ),
                patch(
                    "vntts.prepared_audio_discovery._bound_component",
                    side_effect=AssertionError("admitted component rehashed"),
                ),
                patch(
                    "vntts.prepared_audio_discovery.open_reading_catalog",
                    side_effect=AssertionError("admitted catalog reopened"),
                ),
                patch(
                    "vntts.prepared_audio_discovery.RecordedReferenceSources.capture",
                    side_effect=AssertionError("admitted voices reread"),
                ),
            ):
                self.assertFalse(snapshot.changed())
                refreshed = discover_prepared_audio(
                    game_pack=pack, story_index=story_index, previous=snapshot
                )
            self.assertIs(refreshed.sources[0], snapshot.sources[0])
            self.assertIs(refreshed.catalogs[0], snapshot.catalogs[0])

    def test_explicit_rediscovery_admits_replaced_story_at_the_same_path(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            story = snapshot.sources[0].story_index
            snapshot = discover_prepared_audio(game_pack=pack, story_index=story)
            rows = [json.loads(row) for row in story.read_text().splitlines()]
            rows[1]["speaker"] = "Bea"
            story.write_text("".join(json.dumps(row) + "\n" for row in rows))
            document = json.loads(pack.read_text())
            document["components"]["story_index"]["sha256"] = sha256_file(story)
            pack.write_text(json.dumps(document))
            refreshed = discover_prepared_audio(
                game_pack=pack, story_index=story, previous=None
            )
            self.assertIsNot(refreshed.sources[0], snapshot.sources[0])
            self.assertIsNot(refreshed.catalogs[0], snapshot.catalogs[0])
            self.assertEqual(refreshed.preloader.line_for_id(line_id).speaker, "Bea")
            self.assertEqual(snapshot.preloader.line_for_id(line_id).speaker, "Ada")

    def test_catalog_merge_preserves_qualified_partial_original_evidence(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            partial = replace(
                source.preloader.dialogue[0],
                source_audio_status="available",
                source_audio_id="partial-voice",
                source_audio_completeness="partial",
                source_audio_authoritative=True,
                source_audio_duration_seconds=1.25,
            )
            source = replace(source, preloader=type(source.preloader)((partial,)))
            raw = replace(
                partial,
                source_audio_completeness="unknown",
                source_audio_authoritative=False,
                source_audio_duration_seconds=None,
            )
            catalog = ReadingCatalog(
                source.game_id,
                source.game_name,
                self.revision(source.story_index),
                (raw,),
            )
            snapshot = PreparedAudioSnapshot((source,), catalogs=(catalog,))
            self.assertIs(snapshot.preloader.line_for_id(line_id), partial)
            self.assertFalse(partial.has_original_speech)

    def test_original_iteration_queries_only_originals_and_applies_current_metadata(
        self,
    ):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            original = replace(
                source.preloader.line_for_id(line_id),
                source_audio_status="available",
                source_audio_completeness="full",
                source_audio_authoritative=True,
                source_audio_duration_seconds=1.0,
            )
            source = replace(source, preloader=type(source.preloader)((original,)))
            current = replace(
                original,
                source_audio_status="absent",
                source_audio_id=None,
                source_audio_completeness="unknown",
                source_audio_authoritative=False,
                source_audio_duration_seconds=None,
            )
            catalog = ReadingCatalog(
                source.game_id,
                source.game_name,
                self.revision(source.story_index),
                (current,),
            )
            with (
                patch.object(
                    source.preloader.store,
                    "iter_rows",
                    side_effect=AssertionError("enumerated dialogue"),
                ),
                patch.object(
                    source.preloader.store,
                    "iter_original_rows",
                    side_effect=lambda: iter((original,)),
                ),
                patch.object(
                    catalog.store,
                    "iter_rows",
                    side_effect=AssertionError("enumerated catalog"),
                ),
                patch.object(
                    catalog.store, "iter_original_rows", return_value=iter(())
                ),
            ):
                self.assertEqual(
                    tuple(
                        PreparedAudioSnapshot(
                            (source,)
                        ).preloader.store.iter_original_rows()
                    ),
                    (original,),
                )
                self.assertEqual(
                    tuple(
                        PreparedAudioSnapshot(
                            (source,), catalogs=(catalog,)
                        ).preloader.store.iter_original_rows()
                    ),
                    (),
                )

    def test_replaced_catalog_text_cannot_match_an_obsolete_archive_candidate(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            old = source.preloader.line_for_id(line_id)
            current = replace(old, text="The current canonical text.")
            catalog = ReadingCatalog(
                source.game_id,
                source.game_name,
                self.revision(source.story_index),
                (current,),
            )
            preloader = PreparedAudioSnapshot((source,), catalogs=(catalog,)).preloader
            self.assertIsNone(preloader.resolve_exact(old.speaker, text))
            self.assertIs(
                preloader.resolve_exact(current.speaker, current.text), current
            )

    def test_candidate_lanes_follow_resolved_original_authority(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            absent = source.preloader.line_for_id(line_id)
            full = replace(
                absent,
                source_audio_status="available",
                source_audio_id="original-voice",
                source_audio_completeness="full",
                source_audio_authoritative=True,
                source_audio_duration_seconds=1.0,
            )
            raw = replace(
                full,
                source_audio_completeness="unknown",
                source_audio_authoritative=False,
                source_audio_duration_seconds=None,
            )
            main_story = replace(raw, source_audio_main_story=True)
            for archived, current, expected in (
                (absent, main_story, main_story),
                (full, raw, full),
                (full, absent, absent),
            ):
                with self.subTest(archived=archived, current=current):
                    archived_source = replace(
                        source, preloader=type(source.preloader)((archived,))
                    )
                    catalog = ReadingCatalog(
                        source.game_id,
                        source.game_name,
                        self.revision(source.story_index),
                        (current,),
                    )
                    store = PreparedAudioSnapshot(
                        (archived_source,), catalogs=(catalog,)
                    ).preloader.store
                    rows = store.candidates(text, prefix=False)
                    original = store.candidates(text, prefix=False, original=True)
                    synthesis = store.candidates(text, prefix=False, original=False)
                    self.assertEqual(rows, (expected,))
                    self.assertEqual(
                        original, (expected,) if expected.has_original_speech else ()
                    )
                    self.assertEqual(
                        synthesis, () if expected.has_original_speech else (expected,)
                    )
                    self.assertEqual(
                        {row.line_id for row in rows},
                        {row.line_id for row in (*original, *synthesis)},
                    )

    def test_current_catalog_replaces_obsolete_original_audio_authority(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            original = replace(
                source.preloader.dialogue[0],
                source_audio_status="available",
                source_audio_id="old-voice",
                source_audio_completeness="full",
                source_audio_authoritative=True,
                source_audio_duration_seconds=1.0,
            )
            source = replace(source, preloader=type(source.preloader)((original,)))
            current = replace(
                original,
                source_audio_status="absent",
                source_audio_id=None,
                source_audio_completeness="unknown",
                source_audio_authoritative=False,
                source_audio_duration_seconds=None,
            )
            catalog = ReadingCatalog(
                source.game_id,
                source.game_name,
                self.revision(source.story_index),
                (current,),
            )
            snapshot = PreparedAudioSnapshot((source,), catalogs=(catalog,))
            self.assertIs(snapshot.preloader.line_for_id(line_id), current)
            self.assertTrue(
                snapshot.library_for(lambda _entry: True).has_entry(line_id, text_hash)
            )

    def test_reservation_keeps_archive_entry_without_reading_pcm_or_rediscovering(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            source = snapshot.sources[0]
            source_owner = weakref.ref(source.library)
            allowed = [True]
            library = snapshot.library_for(lambda entry: allowed[0])
            with patch(
                "vntts.generated_audio.read_regular_bytes",
                side_effect=AssertionError("WAV read"),
            ):
                reference = library.reserve(line_id, text_hash)
            self.assertIsNotNone(reference)
            self.assertEqual(reference.line_id, line_id)
            allowed[0] = False
            self.assertIsNone(library.reserve(line_id, text_hash))
            self.assertIsNone(
                PreparedAudioSnapshot()
                .library_for(lambda entry: True)
                .reserve(line_id, text_hash)
            )
            del library, source, snapshot
            gc.collect()
            self.assertIsNone(source_owner())
            prepared, _state = reference.load()
            self.assertIsNotNone(prepared)
            self.assertTrue(prepared.audio_path.samefile(wav))

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
                        self.assertEqual(
                            library.reserve(line_id, text_hash) is not None, eligible
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

    def test_catalog_packs_and_fresh_sqlite_audio_are_discovered_without_full_import(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            pack, line_id, text, text_hash, _wav = write_synthetic_game_pack(assets)
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
            item = queue_item("prepared", character="Ada", text=text)
            item["line_id"] = line_id
            item["queue_id"] = f"{line_id}:{text_hash[:16]}"
            queue = write_queue(directory / "queue.jsonl", [item])
            store.catalog.save_input(
                job.job_id,
                {
                    "identity": identity,
                    "job_id": job.job_id,
                    "queue_sha256": sha256_file(queue),
                    "story_index_sha256": bindings["story_index"]["sha256"],
                    "voice_manifest_sha256": bindings["voice_manifest"]["sha256"],
                },
                directory,
            )
            snapshot = discover_prepared_audio(store)
            self.assertEqual(len(snapshot.sources), 1)
            self.assertFalse(snapshot.changed())
            output = directory.parent / ("generation-output-" + identity[:16])
            generated = run_bulk_generation(
                queue,
                output,
                SyntheticRenderer(),
                provider="synthetic",
                model="synthetic-v1",
            )
            self.assertFalse((output / "live-progress-manifest.json").exists())
            self.assertFalse((output / "manifest.json").exists())
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
            progress_source = next(
                source for source in refreshed.sources if source.pack_manifest is None
            )
            self.assertEqual(progress_source.library.manifest_path, generated.state)
            with patch(
                "vntts.generated_audio.read_regular_bytes",
                side_effect=AssertionError("Discovery must not read WAVs"),
            ):
                progress = discover_prepared_audio(store)
                borrowed = (
                    PreparedAudioSnapshot((progress_source,))
                    .library_for(lambda entry: True)
                    .reserve(line_id, text_hash)
                )
            self.assertIsNotNone(borrowed)
            prepared, _status = borrowed.load()
            self.assertIsNotNone(prepared)
            self.assertTrue(prepared.audio_path.is_relative_to(output.resolve()))
            self.assertEqual(len(progress.sources), 2)
            updated = generation_database.read_state(generated.state)
            updated["updated_at"] = "new committed progress"
            generation_database.write_state(generated.state, updated)
            self.assertTrue(refreshed.changed())
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

    def test_window_title_matches_a_complete_game_name(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            for title in ("", "Other Game", "Synthetic Games"):
                with self.subTest(title=title):
                    self.assertFalse(
                        snapshot.for_settings(
                            AppSettings(game_window_title=title)
                        ).sources
                    )
            selected = snapshot.for_settings(
                AppSettings(game_window_title="Synthetic Game - Player")
            )
            self.assertIsNotNone(selected.preloader.line_for_id(line_id))
            self.assertFalse(
                snapshot.for_settings(
                    AppSettings(
                        game_pack=str(pack.with_name("missing.json")),
                        game_window_title="Synthetic Game",
                    )
                ).sources
            )

    def test_identical_lines_use_the_recording_from_the_selected_game(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first, line_id, _text, text_hash, first_wav = write_synthetic_game_pack(
                root / "first"
            )
            second, _, _, _, second_wav = write_synthetic_game_pack(root / "second")
            document = json.loads(second.read_bytes())
            document["game"]["id"] = "second-game"
            second.write_text(json.dumps(document))
            (first_source,) = discover_prepared_audio(game_pack=first).sources
            (second_source,) = discover_prepared_audio(game_pack=second).sources
            snapshot = PreparedAudioSnapshot((first_source, second_source))

            self.assertIsNone(snapshot.preloader.line_for_id(line_id))
            self.assertIsNone(
                snapshot.library_for(lambda _: True).find(line_id, text_hash)
            )
            by_title = AppSettings(game_window_title="Synthetic Game")
            self.assertFalse(snapshot.for_settings(by_title).sources)
            for settings, expected_wav in (
                (
                    by_title.updated(
                        game_pack=str(first), story_index=str(second_source.story_index)
                    ),
                    first_wav,
                ),
                (
                    by_title.updated(story_index=str(second_source.story_index)),
                    second_wav,
                ),
            ):
                with self.subTest(expected=expected_wav):
                    selected = snapshot.for_settings(settings)
                    self.assertIsNotNone(selected.preloader.line_for_id(line_id))
                    audio = selected.library_for(lambda _: True).find(
                        line_id, text_hash
                    )
                    self.assertEqual(audio.audio_path, expected_wav)
            scoped = snapshot.library_for(
                lambda _: True,
                game_id=second_source.game_id,
                content_sha256=second_source.content_sha256,
            )
            self.assertEqual(scoped.find(line_id, text_hash).audio_path, second_wav)

    def test_explicit_binding_accepts_both_paths_to_the_same_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(root / "pack")
            alias = root / "alias"
            symlink_or_skip(alias, pack.parent, target_is_directory=True)
            alias_pack = alias / pack.name
            for found, selected in ((pack, alias_pack), (alias_pack, pack)):
                with self.subTest(found=found, selected=selected):
                    snapshot = discover_prepared_audio(game_pack=found)
                    story = selected.with_name("story-index.jsonl")
                    by_pack = AppSettings(game_pack=str(selected))
                    by_story = AppSettings(story_index=str(story))
                    for settings in (by_pack, by_story):
                        self.assertIsNotNone(
                            snapshot.for_settings(settings).preloader.line_for_id(
                                line_id
                            )
                        )

                    catalog = open_reading_catalog(found.with_name("story-index.jsonl"))
                    catalog_only = PreparedAudioSnapshot(catalogs=(catalog,))
                    self.assertEqual(
                        catalog_only.for_settings(by_story).catalogs, (catalog,)
                    )

    def test_new_import_is_discovered_without_watching_admitted_story_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "synthetic").mkdir()
            snapshot = discover_prepared_audio(catalog_root=root)
            self.assertFalse(snapshot.changed())
            _pack, line_id, _text, _hash, _wav = write_synthetic_game_pack(
                root / "synthetic" / "synthetic"
            )
            story = root / "synthetic" / "synthetic" / "story-index.jsonl"
            rows = [json.loads(row) for row in story.read_text().splitlines()]
            rows[0]["game_id"] = "synthetic-game"
            story.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertTrue(snapshot.changed())
            refreshed = discover_prepared_audio(catalog_root=root, previous=snapshot)
            self.assertIsNotNone(refreshed.preloader.line_for_id(line_id))
            story_index = refreshed.catalogs[0].story_index
            self.assertNotIn(story_index, dict(refreshed.watched_paths))

    def test_pack_playback_does_not_poll_its_admitted_manifest(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            snapshot = discover_prepared_audio(game_pack=pack)
            source = snapshot.sources[0]
            library = snapshot.library_for(lambda _entry: True)
            with patch(
                "vntts.generated_audio._manifest_signature",
                side_effect=AssertionError("admitted pack manifest polled"),
            ):
                self.assertTrue(library.has_entry(line_id, text_hash))
                self.assertTrue(source.library.has_entry(line_id, text_hash))
                self.assertIsNotNone(library.reserve(line_id, text_hash).load()[0])
                self.assertIsNotNone(source.library.reserve(line_id, text_hash))
                self.assertIsNotNone(library.find(line_id, text_hash))
                self.assertIsNone(source.library.find_live_fallback(line_id, text_hash))
                self.assertIsNone(
                    source.library.find_audio_event_omission(line_id, text_hash)
                )

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
                self.assertIsNone(library.reserve(line_id, text_hash))
                self.assertIsNone(library.find(line_id, text_hash))
            self.assertFalse(library.runtime_progress)

    def test_unchanged_canonical_lines_reuse_audio_across_catalog_revisions(self):
        with TemporaryDirectory() as temporary:
            pack, line_id, _text, text_hash, _wav = write_synthetic_game_pack(
                Path(temporary)
            )
            source = discover_prepared_audio(game_pack=pack).sources[0]
            newer = replace(source, content_sha256="f" * 64)
            snapshot = PreparedAudioSnapshot((source, newer))
            self.assertEqual(
                [row.line_id for row in snapshot.preloader.dialogue], [line_id]
            )
            self.assertIsNotNone(snapshot.preloader.line_for_id(line_id))
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
            self.assertIsNone(
                snapshot.library_for(lambda entry: True).reserve(line_id, text_hash)
            )

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
