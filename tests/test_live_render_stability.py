import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from vntts.live import LiveDialogReader


class CaptureSteps:
    def __init__(self, count, clock):
        self.remaining = count
        self.clock = clock

    def is_set(self):
        return self.remaining == 0

    def wait(self, _interval):
        self.remaining -= 1
        self.clock[0] += 0.3


class LiveRenderStabilityTest(unittest.TestCase):
    def reader(self, **options):
        return LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=Mock(),
            playback_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=Mock(),
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            report_error=Mock(side_effect=AssertionError),
            **options,
        )

    def capture_frames(self, values, *, owners=None, comparator=None):
        clock = [0.0]
        owner = ["line-1"]
        frames = [SimpleNamespace(value=value) for value in values]
        reader = self.reader(
            stable_frame_clock=lambda: clock[0],
            stable_frame_owner=lambda: owner[0],
            frame_fingerprint=lambda frame: frame.value,
            frame_render_fingerprint=lambda frame: frame.value,
            frame_render_equivalent=comparator,
        )
        iterator = iter(enumerate(frames))

        def capture():
            index, frame = next(iterator)
            if owners is not None:
                owner[0] = owners[index]
            return frame

        reader.capture_frame = capture
        reader._run_capture(CaptureSteps(len(frames), clock))
        return reader, frames, clock

    def test_edge_noise_preserves_quiet_time_and_original_anchor(self):
        compared = []

        def equivalent(before, after):
            compared.append((before.value, after.value))
            return abs(before.value - after.value) <= 4

        reader, frames, _clock = self.capture_frames(
            [158, 160, 162, 159], comparator=equivalent
        )
        self.assertIs(reader.render_anchor_frame, frames[0])
        self.assertEqual(compared, [(158, 160), (158, 162), (158, 159)])
        self.assertEqual(
            reader.current_frame_render_quiet_ms(expected_owner="line-1"), 1200
        )
        self.assertTrue(reader.bind_current_frame_route(expected_render_owner="line-1"))
        self.assertIs(reader.routed_frame, frames[-1])

    def test_cumulative_changes_do_not_drift_from_original_anchor(self):
        reader, frames, _clock = self.capture_frames(
            [158, 162, 166],
            comparator=lambda before, after: abs(before.value - after.value) <= 4,
        )
        self.assertIs(reader.render_anchor_frame, frames[2])
        self.assertAlmostEqual(reader.latest_render_changed_at, 0.6)
        self.assertIsNone(reader.current_frame_render_quiet_ms(expected_owner="line-1"))

    def test_new_owner_resets_quiet_even_when_pixels_match(self):
        comparator = Mock(return_value=True)
        reader, frames, _clock = self.capture_frames(
            [158, 158], owners=["line-1", "line-2"], comparator=comparator
        )
        comparator.assert_not_called()
        self.assertIs(reader.render_anchor_frame, frames[1])
        self.assertEqual(reader.latest_render_owner, "line-2")
        self.assertFalse(
            reader.bind_current_frame_route(expected_render_owner="line-1")
        )

    def test_reset_during_comparison_invalidates_old_quiet_evidence(self):
        clock = [0.0]
        reader = self.reader(
            stable_frame_clock=lambda: clock[0],
            stable_frame_owner=lambda: "line-1",
            frame_fingerprint=lambda frame: frame.value,
        )
        frames = iter([SimpleNamespace(value=1), SimpleNamespace(value=2)])
        reader.capture_frame = lambda: next(frames)

        def compare(_before, _after):
            with reader.state_lock:
                reader._reset_render_observation()
            return True

        reader.frame_render_equivalent = compare
        reader._run_capture(CaptureSteps(2, clock))
        self.assertAlmostEqual(reader.latest_render_changed_at, 0.3)
        self.assertIsNone(reader.current_frame_render_quiet_ms(expected_owner="line-1"))

    def test_render_hash_collision_cannot_hide_new_punctuation(self):
        clock = [0.0]
        compared = Mock(return_value=False)
        reader = self.reader(
            stable_frame_clock=lambda: clock[0],
            stable_frame_owner=lambda: "line-1",
            frame_fingerprint=lambda frame: frame.value,
            frame_render_fingerprint=lambda _frame: "same-dilated-mask",
            frame_render_equivalent=compared,
        )
        first, second = SimpleNamespace(value=1), SimpleNamespace(value=2)
        frames = iter([first, second])
        reader.capture_frame = lambda: next(frames)
        reader._run_capture(CaptureSteps(2, clock))
        compared.assert_called_once_with(first, second)
        self.assertIs(reader.render_anchor_frame, second)
        self.assertAlmostEqual(reader.latest_render_changed_at, 0.3)
        self.assertIsNone(reader.current_frame_render_quiet_ms(expected_owner="line-1"))

    def test_bind_rechecks_new_render_activity_after_quiet_probe(self):
        reader, _frames, clock = self.capture_frames([1, 1, 1, 1])
        self.assertEqual(
            reader.current_frame_render_quiet_ms(expected_owner="line-1"), 1200
        )
        reader.latest_render_changed_at = clock[0]
        reader.latest_frame_fingerprint = 2
        self.assertFalse(
            reader.bind_current_frame_route(expected_render_owner="line-1")
        )
        self.assertIsNone(reader.routed_frame)

    def test_quiet_bind_rejects_stop_pause_focus_and_uncertainty(self):
        for state in ("stop", "paused", "focus", "uncertain"):
            with self.subTest(state=state):
                reader, _frames, _clock = self.capture_frames([1, 1, 1, 1])
                if state == "stop":
                    reader.stop_event.set()
                elif state == "paused":
                    reader.paused = True
                elif state == "focus":
                    reader.focus_probe = lambda: False
                else:
                    reader.observation_uncertain = True
                self.assertFalse(
                    reader.bind_current_frame_route(expected_render_owner="line-1")
                )
                self.assertIsNone(reader.routed_frame)
