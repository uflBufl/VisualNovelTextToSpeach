import hashlib
import json
import unittest
import wave
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from PIL import Image, ImageDraw
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import text_sha256, write_generated_audio_manifest

from vntts.live_replay import (
    LiveReplayRunner,
    ReplayFrameSource,
    _load_frame,
    load_live_replay_corpus,
    main,
)


class LiveReplayTest(unittest.TestCase):
    def create_corpus(self, directory):
        directory = Path(directory)
        first = Image.new("RGB", (320, 120), "black")
        first.putpixel((0, 0), (255, 0, 0))
        ImageDraw.Draw(first).rectangle((20, 15, 200, 35), fill="white")
        first.save(directory / "first.png")
        second = Image.new("RGB", (320, 120), "black")
        second.putpixel((0, 0), (0, 0, 255))
        ImageDraw.Draw(second).rectangle((50, 65, 300, 90), fill="white")
        second.save(directory / "second.png")
        path = directory / "corpus.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "Rhiannon deterministic smoke",
                    "dialogue": [
                        {
                            "frames": ["first.png", "first.png"],
                            "character": "Rhiannon",
                            "text": "I, erhm ...",
                            "line_id": "reverse1999:rhiannon:1",
                            "source_audio_status": "available",
                            "source_audio_duration_seconds": 0.001,
                            "expected_source": "game",
                        },
                        {
                            "frames": ["second.png", "second.png"],
                            "character": "Hotelier",
                            "text": "So, you haven't any then.",
                            "line_id": "reverse1999:rhiannon:2",
                            "source_audio_status": "missing",
                            "expected_source": "live:replay-live-tts",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def recognize(frame):
        marker = frame.image.getpixel((0, 0))
        if marker == (255, 0, 0):
            return "Rhiannon", "I, erhm ..."
        return "Hotelier", "So, you haven't any then."

    def test_replays_fingerprint_route_playback_and_auto_advance_pipeline(self):
        with TemporaryDirectory() as temporary_directory:
            corpus = load_live_replay_corpus(self.create_corpus(temporary_directory))

            report = LiveReplayRunner(
                corpus,
                recognizer=self.recognize,
                interval_seconds=0.002,
                timeout_seconds=2,
            ).run()

        self.assertTrue(report["successful"], report)
        self.assertEqual(
            report["route_sources"],
            ["game", "live:replay-live-tts"],
        )
        self.assertEqual(report["advance_requests"], 2)
        first_stages = {event["stage"] for event in report["timelines"][0]["events"]}
        self.assertEqual(
            first_stages,
            {
                "capture",
                "ocr",
                "stable-text",
                "route-decision",
                "voice-resolution",
                "generation-start",
                "playback-completion",
                "playback-outcome",
                "key-dispatch",
                "confirmed-next-dialogue",
            },
        )

    def test_unobserved_game_audio_completion_blocks_replay_auto_advance(self):
        with TemporaryDirectory() as temporary_directory:
            path = self.create_corpus(temporary_directory)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["dialogue"][0].pop("source_audio_duration_seconds")
            path.write_text(json.dumps(document), encoding="utf-8")
            corpus = load_live_replay_corpus(path)

            report = LiveReplayRunner(
                corpus,
                recognizer=self.recognize,
                interval_seconds=0.002,
                timeout_seconds=0.1,
            ).run()

        self.assertFalse(report["successful"])
        self.assertEqual(report["route_sources"], ["game"])
        self.assertEqual(report["advance_requests"], 0)

    def test_advance_waits_until_every_declared_frame_is_acknowledged(self):
        with TemporaryDirectory() as temporary_directory:
            corpus = load_live_replay_corpus(self.create_corpus(temporary_directory))
            source = ReplayFrameSource(corpus.dialogue)
            first = source.acknowledge(source.capture())
            started = Event()
            result = []

            def advance():
                started.set()
                result.append(source.advance())

            thread = Thread(target=advance)
            thread.start()
            self.assertTrue(started.wait(1))
            thread.join(0.05)
            self.assertTrue(thread.is_alive())

            second = source.acknowledge(source.capture())
            thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [True])
        self.assertTrue(first["consumed"])
        self.assertEqual(first["frame_index"], 1)
        self.assertTrue(second["consumed"])
        self.assertEqual(second["frame_index"], 2)

    def test_stop_unblocks_an_incomplete_frame_wait_without_completion(self):
        with TemporaryDirectory() as temporary_directory:
            corpus = load_live_replay_corpus(self.create_corpus(temporary_directory))
            source = ReplayFrameSource(corpus.dialogue)
            source.acknowledge(source.capture())
            started = Event()
            result = []

            def advance():
                started.set()
                result.append(source.advance())

            thread = Thread(target=advance)
            thread.start()
            self.assertTrue(started.wait(1))
            thread.join(0.05)
            self.assertTrue(thread.is_alive())

            source.stop()
            thread.join(1)
            consumption = source.snapshot()

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(source.completed.is_set())
        self.assertFalse(consumption["complete"])
        self.assertLess(consumption["consumed_count"], consumption["declared_count"])

    def test_hundred_declared_frames_are_all_consumed_before_success(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame = root / "frame.png"
            Image.new("RGB", (20, 20), "black").save(frame)
            frame_sha256 = sha256_file(frame)
            frame_spec = {
                "path": frame.name,
                "sha256": frame_sha256,
                "observed_character": "Rhiannon",
                "observed_text": "I, erhm ...",
            }
            path = root / "corpus.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fixture_kind": "declared-frame-ledger-regression",
                        "dialogue": [
                            {
                                "frames": [dict(frame_spec) for _index in range(100)],
                                "character": "Rhiannon",
                                "text": "I, erhm ...",
                                "line_id": "fixture:rhiannon:100",
                                "source_audio_status": "available",
                                "source_audio_duration_seconds": 0.001,
                                "expected_source": "game",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = LiveReplayRunner(
                load_live_replay_corpus(path),
                interval_seconds=0.001,
                timeout_seconds=3,
            ).run()

        consumption = report["media_integrity"]["frame_consumption"]
        consumed_events = [
            event
            for event in report["media_integrity"]["recognized_frames"]
            if event["consumed"]
        ]
        self.assertTrue(report["successful"], report)
        self.assertTrue(consumption["complete"])
        self.assertEqual(consumption["declared_count"], 100)
        self.assertEqual(consumption["consumed_count"], 100)
        self.assertEqual(
            [event["frame_index"] for event in consumed_events],
            list(range(1, 101)),
        )
        self.assertTrue(
            all(
                event["path"] == "frame.png" and event["sha256"] == frame_sha256
                for event in consumed_events
            )
        )

    def test_cli_rejects_an_empty_corpus(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "empty.json"
            path.write_text(
                json.dumps({"schema_version": 1, "dialogue": []}),
                encoding="utf-8",
            )
            errors = StringIO()

            with redirect_stderr(errors):
                exit_code = main([str(path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("no dialogue entries", errors.getvalue())

    def test_representative_matrix_gates_prefixes_and_routes_exact_media(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame_specs = []
            observations = [
                ("Rhiannon", "I, erhm ..."),
                ("Hotelier", "A single room will be four coins"),
                ("Hotelier", "A single room will be four coins per night."),
                ("Adar Llwch Gwin Fledgling", "The old forest remembers every"),
                ("Narrator", "Night settles over the lake."),
            ]
            for observation_index, (character, text) in enumerate(observations):
                for repeat in range(4):
                    marker = observation_index * 4 + repeat
                    image = Image.new("RGB", (80, 40), "black")
                    left = (marker * 11) % 60
                    ImageDraw.Draw(image).rectangle(
                        (left, 4, min(79, left + 18), 35), fill="white"
                    )
                    frame_path = root / f"frame-{marker}.png"
                    image.save(frame_path)
                    frame_specs.append(
                        {
                            "path": frame_path.name,
                            "sha256": sha256_file(frame_path),
                            "observed_character": character,
                            "observed_text": text,
                        }
                    )

            generated_text = "The old forest remembers every footstep."
            generated_wav = root / "generated.wav"
            with wave.open(str(generated_wav), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24_000)
                output.writeframes(b"\0\0\1\0\0\0")
            generated_wav_sha256 = sha256_file(generated_wav)
            manifest = root / "generated.json"
            write_generated_audio_manifest(
                manifest,
                {"fixture": "representative-device-free"},
                [
                    {
                        "line_id": "fixture:fledgling:1",
                        "text_sha256": text_sha256(generated_text),
                        "audio": generated_wav.name,
                        "audio_format": "wav-pcm16-mono",
                        "audio_sha256": generated_wav_sha256,
                        "sample_rate": 24_000,
                        "sample_count": 3,
                    }
                ],
            )
            manifest_sha256 = sha256_file(manifest)
            path = root / "corpus.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Representative Rhiannon route matrix",
                        "fixture_kind": "representative-declared-observations",
                        "generated_audio_manifest": {
                            "path": manifest.name,
                            "sha256": manifest_sha256,
                        },
                        "dialogue": [
                            {
                                "frames": frame_specs[0:4],
                                "character": "Rhiannon",
                                "text": "I, erhm ...",
                                "line_id": "fixture:rhiannon:1",
                                "source_audio_status": "available",
                                "source_audio_duration_seconds": 0.001,
                                "expected_source": "game",
                            },
                            {
                                "frames": frame_specs[4:12],
                                "character": "Hotelier",
                                "text": "A single room will be four coins per night.",
                                "line_id": "fixture:hotelier:1",
                                "source_audio_status": "missing",
                                "expected_source": "live:replay-live-tts",
                            },
                            {
                                "frames": frame_specs[12:16],
                                "character": "Adar Llwch Gwin Fledgling",
                                "text": generated_text,
                                "line_id": "fixture:fledgling:1",
                                "source_audio_status": "missing",
                                "expected_source": "generated",
                            },
                            {
                                "frames": frame_specs[16:20],
                                "character": "Narrator",
                                "text": "Night settles over the lake.",
                                "line_id": "fixture:narrator:1",
                                "source_audio_status": "missing",
                                "expected_source": "live:replay-live-tts",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = LiveReplayRunner(
                load_live_replay_corpus(path),
                interval_seconds=0.002,
                timeout_seconds=3,
            ).run()

        self.assertTrue(report["successful"], report)
        self.assertEqual(
            report["route_sources"],
            ["game", "live:replay-live-tts", "generated", "live:replay-live-tts"],
        )
        self.assertEqual(report["advance_requests"], 4)
        self.assertEqual(len(report["media_integrity"]["frame_sha256s"]), 20)
        frame_consumption = report["media_integrity"]["frame_consumption"]
        self.assertTrue(frame_consumption["complete"])
        self.assertEqual(frame_consumption["declared_count"], 20)
        self.assertEqual(frame_consumption["consumed_count"], 20)
        generated = report["media_integrity"]["generated_playback"]
        self.assertEqual(generated[0]["sample_count"], 3)
        self.assertEqual(len(generated[0]["pcm_sha256"]), 64)
        self.assertEqual(report["fixture_kind"], "representative-declared-observations")
        self.assertEqual(
            report["provenance"]["generated_audio_manifest_sha256"],
            manifest_sha256,
        )
        self.assertEqual(
            report["provenance"]["recognition_sources"],
            ["declared-observation"],
        )
        self.assertEqual(
            report["provenance"]["generated_audio_artifacts"],
            [{"path": "generated.wav", "sha256": generated_wav_sha256}],
        )

    def test_checksum_bound_fixture_rejects_modified_frame(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame = root / "frame.png"
            Image.new("RGB", (20, 20), "black").save(frame)
            path = root / "corpus.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dialogue": [
                            {
                                "frames": [
                                    {
                                        "path": frame.name,
                                        "sha256": hashlib.sha256(b"stale").hexdigest(),
                                        "observed_character": "Rhiannon",
                                        "observed_text": "I, erhm ...",
                                    }
                                ],
                                "character": "Rhiannon",
                                "text": "I, erhm ...",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "checksum does not match"):
                load_live_replay_corpus(path)

    def test_frame_is_decoded_from_the_exact_bytes_that_were_hashed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame = root / "frame.png"
            Image.new("RGB", (20, 20), "black").save(frame)
            expected_sha256 = sha256_file(frame)
            original_open = Path.open
            swapped = False

            class SwapAfterRead:
                def __init__(self, source):
                    self.source = source

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.source.__exit__(*args)

                def fileno(self):
                    return self.source.fileno()

                def read(self):
                    nonlocal swapped
                    payload = self.source.read()
                    swapped = True
                    Image.new("RGB", (20, 20), "white").save(frame)
                    return payload

            def open_with_swap(path, *args, **kwargs):
                source = original_open(path, *args, **kwargs)
                if path.resolve() == frame.resolve() and "r" in args[0]:
                    return SwapAfterRead(source)
                return source

            with patch.object(Path, "open", open_with_swap):
                captured, relative_path, digest, source = _load_frame(
                    root,
                    {
                        "path": frame.name,
                        "sha256": expected_sha256,
                        "observed_character": "Rhiannon",
                        "observed_text": "I, erhm ...",
                    },
                    None,
                )

        self.assertTrue(swapped)
        self.assertEqual(relative_path, "frame.png")
        self.assertEqual(digest, expected_sha256)
        self.assertEqual(captured.image.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(source, "declared-observation")

    def test_declared_observation_requires_a_checksum(self):
        with TemporaryDirectory() as temporary_directory:
            path = self.create_corpus(temporary_directory)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["dialogue"][0]["frames"] = [
                {
                    "path": "first.png",
                    "observed_character": "Rhiannon",
                    "observed_text": "I, erhm ...",
                }
            ]
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "observation sha256"):
                load_live_replay_corpus(path)

    def test_serialized_media_paths_are_contained_and_not_symlinks(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root.parent / f"{root.name}-outside.png"
            Image.new("RGB", (20, 20), "black").save(outside)
            try:
                path = self.create_corpus(root)
                document = json.loads(path.read_text(encoding="utf-8"))
                document["dialogue"][0]["frames"] = [f"../{outside.name}"]
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "contained relative path"):
                    load_live_replay_corpus(path)

                link = root / "linked.png"
                link.symlink_to(outside)
                document["dialogue"][0]["frames"] = [link.name]
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must not contain symlinks"):
                    load_live_replay_corpus(path)

                outside_manifest = outside.with_suffix(".json")
                outside_manifest.write_text("{}", encoding="utf-8")
                document["dialogue"][0]["frames"] = ["first.png"]
                document["generated_audio_manifest"] = {
                    "path": f"../{outside_manifest.name}",
                    "sha256": sha256_file(outside_manifest),
                }
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "contained relative path"):
                    load_live_replay_corpus(path)

                manifest_link = root / "linked-generated.json"
                manifest_link.symlink_to(outside_manifest)
                document["generated_audio_manifest"]["path"] = manifest_link.name
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must not contain symlinks"):
                    load_live_replay_corpus(path)

                outside_audio = outside.with_suffix(".wav")
                outside_audio.write_bytes(b"bound-audio")
                generated_manifest = root / "generated.json"
                generated_document = {
                    "schema": "vntts.generated-audio",
                    "schema_version": 1,
                    "entry_count": 1,
                    "entries": [
                        {
                            "line_id": "fixture:1",
                            "text_sha256": hashlib.sha256(b"text").hexdigest(),
                            "audio": f"../{outside_audio.name}",
                            "audio_format": "wav-pcm16-mono",
                            "audio_sha256": sha256_file(outside_audio),
                            "sample_rate": 24_000,
                            "sample_count": 1,
                        }
                    ],
                }
                generated_manifest.write_text(
                    json.dumps(generated_document), encoding="utf-8"
                )
                document["generated_audio_manifest"] = {
                    "path": generated_manifest.name,
                    "sha256": sha256_file(generated_manifest),
                }
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "contained relative path"):
                    load_live_replay_corpus(path)

                audio_link = root / "linked.wav"
                audio_link.symlink_to(outside_audio)
                generated_document["entries"][0]["audio"] = audio_link.name
                generated_manifest.write_text(
                    json.dumps(generated_document), encoding="utf-8"
                )
                document["generated_audio_manifest"]["sha256"] = sha256_file(
                    generated_manifest
                )
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must not contain symlinks"):
                    load_live_replay_corpus(path)
            finally:
                outside.unlink(missing_ok=True)
                outside.with_suffix(".json").unlink(missing_ok=True)
                outside.with_suffix(".wav").unlink(missing_ok=True)

    def test_generated_manifest_and_wav_remain_bound_after_corpus_load(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame = root / "frame.png"
            Image.new("RGB", (20, 20), "black").save(frame)
            text = "The old forest remembers every footstep."
            audio = root / "generated.wav"
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24_000)
                output.writeframes(b"\0\0\1\0\0\0")
            manifest = root / "generated.json"
            write_generated_audio_manifest(
                manifest,
                {"fixture": "bound"},
                [
                    {
                        "line_id": "fixture:generated:1",
                        "text_sha256": text_sha256(text),
                        "audio": audio.name,
                        "audio_format": "wav-pcm16-mono",
                        "audio_sha256": sha256_file(audio),
                        "sample_rate": 24_000,
                        "sample_count": 3,
                    }
                ],
            )
            corpus_path = root / "corpus.json"
            corpus_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fixture_kind": "declared-observation",
                        "generated_audio_manifest": {
                            "path": manifest.name,
                            "sha256": sha256_file(manifest),
                        },
                        "dialogue": [
                            {
                                "frames": [
                                    {
                                        "path": frame.name,
                                        "sha256": sha256_file(frame),
                                        "observed_character": "Rhiannon",
                                        "observed_text": text,
                                    }
                                ],
                                "character": "Rhiannon",
                                "text": text,
                                "line_id": "fixture:generated:1",
                                "source_audio_status": "missing",
                                "expected_source": "generated",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            corpus = load_live_replay_corpus(corpus_path)

            original_audio = audio.read_bytes()
            audio.write_bytes(original_audio + b"changed")
            with self.assertRaisesRegex(ValueError, "checksum does not match|changed"):
                LiveReplayRunner(corpus).run()

            audio.write_bytes(original_audio)
            corpus = load_live_replay_corpus(corpus_path)
            manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_document["mutated_after_load"] = True
            manifest.write_text(json.dumps(manifest_document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                LiveReplayRunner(corpus).run()


if __name__ == "__main__":
    unittest.main()
