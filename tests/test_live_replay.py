import json
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

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


if __name__ == "__main__":
    unittest.main()
