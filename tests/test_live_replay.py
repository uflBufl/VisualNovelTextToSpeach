import hashlib
import json
import unittest
import wave
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import text_sha256, write_generated_audio_manifest

from vntts.live_replay import LiveReplayRunner, load_live_replay_corpus, main


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
                        "audio_sha256": sha256_file(generated_wav),
                        "sample_rate": 24_000,
                        "sample_count": 3,
                    }
                ],
            )
            path = root / "corpus.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Representative Rhiannon route matrix",
                        "generated_audio_manifest": manifest.name,
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
        generated = report["media_integrity"]["generated_playback"]
        self.assertEqual(generated[0]["sample_count"], 3)
        self.assertEqual(len(generated[0]["pcm_sha256"]), 64)

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


if __name__ == "__main__":
    unittest.main()
