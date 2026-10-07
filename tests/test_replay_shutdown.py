import gc
import json
import unittest
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from tests import test_live_replay as fixtures
from vntts import live_replay
from vntts.live import LiveDialogReader


class BlockedReplay:
    def __init__(self, corpus, stage):
        self.corpus = corpus
        self.stage = stage
        self.entered, self.release, self.returned = Event(), Event(), Event()
        self.owners, self.readers, self.executors, self.drainers = [], [], [], []
        self.observed, self.failures = [], []
        self.original_owner = live_replay._ReplaySnapshots
        self.original_executor = live_replay.ThreadPoolExecutor
        self.original_source = live_replay.ReplayFrameSource
        self.driver = Thread(target=self.run)

    def owner_factory(self):
        owner = self.original_owner()
        self.owners.append(owner)
        return owner

    def reader_factory(self, **options):
        reader = LiveDialogReader(**options)
        self.readers.append(reader)
        return reader

    def executor_factory(self, **options):
        executor = self.original_executor(**options)
        self.executors.append(executor)
        return executor

    def source_factory(self, *args, **options):
        source = self.original_source(*args, **options)
        source.completed.wait = self.wait_for_ocr
        return source

    def wait_for_ocr(self, _timeout):
        if not self.entered.wait(3):
            raise TimeoutError("Replay worker never entered")
        return False

    def drainer_factory(self, **options):
        thread = Thread(**options)
        self.drainers.append(thread)
        return thread

    def recognize(self, _frame):
        if self.stage == "ocr":
            self.observe_worker()
            return None, ""
        return "Rhiannon", "Hello."

    def prepare(self, original, backend, character, text):
        if self.stage == "speech":
            self.observe_worker()
        return original(backend, character, text)

    def observe_worker(self):
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("test did not release worker")
        owner = self.owners[0]
        files = [
            file
            for root in owner.directories
            for file in Path(root).rglob("*")
            if file.is_file()
        ]
        self.observed.append(
            (
                bool(files),
                all(file.read_bytes() for file in files),
                owner.wait_for.is_set(),
            )
        )

    def run(self):
        try:
            live_replay.LiveReplayRunner(
                self.corpus,
                recognizer=self.recognize,
                interval_seconds=0.001,
                timeout_seconds=0.01,
                audio_source_policy="live-tts-only",
            ).run()
        except BaseException as error:
            self.failures.append(error)
        finally:
            self.returned.set()

    def close(self):
        self.release.set()
        for reader in self.readers:
            reader.shutdown()
        self.driver.join(3)
        for executor in self.executors:
            executor.shutdown(wait=True, cancel_futures=True)
        for owner in self.owners:
            if owner.wait_for is not None:
                owner.wait_for.wait(3)
        for drainer in self.drainers:
            drainer.join(3)


class ReplayShutdownTest(unittest.TestCase):
    def test_failed_stop_retains_real_snapshots_until_workers_finish(self):
        normal_wait = LiveDialogReader.wait
        normal_prepare = live_replay.ReplayLiveSpeechBackend.prepare_playback

        def bounded_wait(reader, timeout_seconds=None):
            self.assertEqual(timeout_seconds, 5.0)
            return normal_wait(reader, timeout_seconds=0.02)

        for sequence, stage in product((False, True), ("ocr", "speech")):
            with (
                self.subTest(sequence=sequence, stage=stage),
                TemporaryDirectory() as directory,
            ):
                path = self.create_corpus(directory, sequence=sequence, stage=stage)
                replay = BlockedReplay(live_replay.load_live_replay_corpus(path), stage)
                primary = OSError("reader stop interrupted before signaling")
                with (
                    patch.multiple(
                        live_replay,
                        _ReplaySnapshots=replay.owner_factory,
                        LiveDialogReader=replay.reader_factory,
                        ThreadPoolExecutor=replay.executor_factory,
                        ReplayFrameSource=replay.source_factory,
                        Thread=replay.drainer_factory,
                    ),
                    patch.object(LiveDialogReader, "stop", side_effect=primary),
                    patch.object(LiveDialogReader, "wait", bounded_wait),
                    patch.object(
                        live_replay.ReplayLiveSpeechBackend,
                        "prepare_playback",
                        lambda backend, character, text: replay.prepare(
                            normal_prepare, backend, character, text
                        ),
                    ),
                    patch.object(
                        live_replay.ReplayLiveSpeechBackend, "play_prepared"
                    ) as playback,
                ):
                    replay.driver.start()
                    try:
                        self.assertTrue(
                            replay.returned.wait(3), "failed stop blocked run"
                        )
                        self.assertEqual(replay.failures, [primary])
                        owner = replay.owners[0]
                        self.assertIsNotNone(owner.wait_for)
                        self.assertFalse(owner.wait_for.is_set())
                        self.assertTrue(replay.readers[0].shutdown_started)
                        self.assertTrue(replay.readers[0].stop_event.is_set())
                        roots = tuple(owner.directories)
                        self.assertEqual(len(roots), 2 if sequence else 1)
                        gc.collect()
                        self.assertTrue(all(Path(root).exists() for root in roots))
                        replay.close()
                        self.assertTrue(owner.wait_for.is_set())
                        self.assertFalse(replay.driver.is_alive())
                        self.assertTrue(
                            all(not thread.is_alive() for thread in replay.drainers)
                        )
                        self.assertEqual(replay.observed, [(True, True, False)])
                        playback.assert_not_called()
                        self.assertEqual(owner.directories, [])
                        self.assertTrue(all(not Path(root).exists() for root in roots))
                    finally:
                        replay.close()

    def create_corpus(self, directory, *, sequence, stage):
        line = {
            "line_id": "story:shutdown:1",
            "chapter": "1",
            "sequence": 1,
            "speaker": "Rhiannon",
            "text": "Hello.",
            "source_audio_status": "absent",
        }
        path = fixtures.LiveReplayTest().create_sequence_corpus(
            directory,
            mode="shadow" if stage == "ocr" else "audio-auto",
            story_lines=[line],
            events=[
                {
                    "event_id": "shutdown-1",
                    "sequence": 1,
                    "kind": "speech",
                    "control": "terminal",
                    "successors": [],
                    "line_id": line["line_id"],
                }
            ],
            dialogue_line_ids=(line["line_id"],),
            generated_line_id=line["line_id"],
            expected_counts={
                "ocr_calls": 1,
                "bounded_recoveries": 0,
                "key_dispatch_attempts": 0,
                "confirmed_key_dispatches": 0,
            },
        )
        if not sequence:
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("live_sequence")
            document["schema_version"] = 1
            path.write_text(json.dumps(document), encoding="utf-8")
        return path
