import json
import subprocess
import time
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.native_resources import NativeResourceSampler


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class FakeProcess:
    def __init__(self, pid, samples):
        self.pid = pid
        self.samples = iter(samples)
        self.current = None

    def memory_info(self):
        self.current = next(self.samples)
        return SimpleNamespace(rss=self.current[0])

    def cpu_times(self):
        return SimpleNamespace(user=self.current[1], system=self.current[2])

    def num_threads(self):
        return self.current[3]


class NativeResourceSamplerTest(unittest.TestCase):
    def nvidia_run(self, output, returncode=0):
        def run(*_args, **kwargs):
            kwargs["stdout"].write(output.encode("utf-8"))
            return SimpleNamespace(returncode=returncode)

        return run

    def fake_psutil(
        self, native_samples=((100, 1.0, 0.5, 3),), host_samples=((50, 0, 0, 1),)
    ):
        native = FakeProcess(42, native_samples)
        host = FakeProcess(1, host_samples)
        return SimpleNamespace(
            Process=lambda pid: native if pid == 42 else host,
            virtual_memory=lambda: SimpleNamespace(total=1000, available=400),
            swap_memory=lambda: SimpleNamespace(used=20),
            cpu_count=lambda logical: 8 if logical else None,
            NoSuchProcess=NoSuchProcess,
            AccessDenied=AccessDenied,
        )

    def test_collects_process_system_and_board_metrics(self):
        psutil = self.fake_psutil(
            native_samples=((100, 1.0, 0.5, 3), (250, 1.4, 0.7, 5)),
            host_samples=((50, 0, 0, 1), (80, 0, 0, 1)),
        )
        output = "0, 555.42, 25, 100, 800\n1, 555.42, 90, 200, 1600\n"
        with (
            patch("vntts.native_resources.psutil", psutil),
            patch("vntts.native_resources.os.getpid", return_value=1),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run(output),
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(10.0)
            sampler._next_gpu_at = 0
            sampler._sample(12.0)
            summary = sampler.finish()

        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["coverage_seconds"], 2.0)
        self.assertAlmostEqual(summary["native_process"]["cpu_seconds_delta"], 0.6)
        self.assertAlmostEqual(summary["native_process"]["avg_cores_used"], 0.3)
        self.assertEqual(summary["native_process"]["rss_bytes_peak"], 250)
        self.assertEqual(summary["native_process"]["thread_count_peak"], 5)
        self.assertEqual(summary["host_app"]["rss_bytes_peak"], 80)
        self.assertEqual(summary["system"]["ram_total_bytes"], 1000)
        self.assertEqual(summary["system"]["ram_available_bytes_min"], 400)
        self.assertEqual(summary["system"]["swap_used_bytes"], 20)
        self.assertEqual(summary["system"]["cpu_logical_count"], 8)
        self.assertEqual(summary["gpu"]["status"], "available")
        self.assertEqual(
            summary["gpu"]["boards"],
            [
                {
                    "logical_index": 0,
                    "driver_version": "555.42",
                    "utilization_percent_peak": 25.0,
                    "utilization_percent_mean": 25.0,
                    "utilization_sample_count": 2,
                    "vram_used_bytes_peak": 100 * 1024 * 1024,
                    "vram_total_bytes": 800 * 1024 * 1024,
                },
                {
                    "logical_index": 1,
                    "driver_version": "555.42",
                    "utilization_percent_peak": 90.0,
                    "utilization_percent_mean": 90.0,
                    "utilization_sample_count": 2,
                    "vram_used_bytes_peak": 200 * 1024 * 1024,
                    "vram_total_bytes": 1600 * 1024 * 1024,
                },
            ],
        )
        json.dumps(summary)

    def test_marks_missing_or_timeout_gpu_once_and_keeps_na_values_null(self):
        for failure, expected in (
            (FileNotFoundError(), "missing"),
            (subprocess.TimeoutExpired("nvidia-smi", 1), "timeout"),
        ):
            with self.subTest(expected=expected):
                with (
                    patch("vntts.native_resources.psutil", None),
                    patch(
                        "vntts.native_resources.subprocess.run", side_effect=failure
                    ) as run,
                ):
                    sampler = NativeResourceSampler(42)
                    sampler._sample(1.0)
                    sampler._sample(4.0)
                    summary = sampler.finish()

            self.assertEqual(run.call_count, 1)
            self.assertEqual(summary["gpu"]["status"], expected)
            self.assertIsNone(summary["native_process"]["rss_bytes_peak"])
            self.assertIsNone(summary["system"]["ram_total_bytes"])
            self.assertEqual(summary["gpu"]["boards"], [])

    def test_pid_exit_and_finish_cancel_the_owned_worker(self):
        sampled = Event()

        def exited(_pid):
            sampled.set()
            raise NoSuchProcess()

        psutil = SimpleNamespace(
            Process=Mock(side_effect=exited),
            virtual_memory=Mock(return_value=SimpleNamespace(total=1, available=1)),
            swap_memory=Mock(return_value=SimpleNamespace(used=0)),
            NoSuchProcess=NoSuchProcess,
            AccessDenied=AccessDenied,
        )
        with (
            patch("vntts.native_resources.psutil", psutil),
            patch(
                "vntts.native_resources.subprocess.run", side_effect=FileNotFoundError()
            ),
        ):
            sampler = NativeResourceSampler(42).start()
            self.assertTrue(sampled.wait(1))
            summary = sampler.finish()

        self.assertEqual(summary["native_process"]["status"], "exited")
        self.assertTrue(sampler._stop.is_set())
        self.assertFalse(sampler._worker.is_alive())

    def test_single_cpu_sample_reports_unknown_delta_as_null(self):
        with (
            patch("vntts.native_resources.psutil", self.fake_psutil()),
            patch(
                "vntts.native_resources.subprocess.run", side_effect=FileNotFoundError()
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            summary = sampler.finish()

        self.assertIsNone(summary["native_process"]["cpu_seconds_delta"])
        self.assertIsNone(summary["native_process"]["avg_cores_used"])

    def test_gpu_probe_is_bounded_private_and_limited_to_eight_boards(self):
        psutil = self.fake_psutil()
        output = "\n".join(f"{index}, 555.42, 1, 2, 3" for index in range(10))
        with (
            patch("vntts.native_resources.psutil", psutil),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run(output),
            ) as run,
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            summary = sampler.finish()

        self.assertEqual(len(summary["gpu"]["boards"]), 8)
        self.assertNotIn("pid", json.dumps(summary))
        self.assertNotIn("stdout", json.dumps(summary))
        self.assertEqual(run.call_args.kwargs["timeout"], 1.0)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertEqual(
            run.call_args.args[0],
            [
                "nvidia-smi",
                "--query-gpu=index,driver_version,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
        )

    def test_gpu_partial_values_and_invalid_ranges_do_not_drop_boards(self):
        output = "0, 555.42, N/A, 100, N/A\n1, 555.42, 101, -1, -2\n"
        with (
            patch("vntts.native_resources.psutil", self.fake_psutil()),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run(output),
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            summary = sampler.finish()

        self.assertEqual(summary["gpu"]["status"], "partial")
        self.assertEqual(
            summary["gpu"]["boards"],
            [
                {
                    "logical_index": 0,
                    "driver_version": "555.42",
                    "utilization_percent_peak": None,
                    "utilization_percent_mean": None,
                    "utilization_sample_count": 0,
                    "vram_used_bytes_peak": 100 * 1024 * 1024,
                    "vram_total_bytes": None,
                },
                {
                    "logical_index": 1,
                    "driver_version": "555.42",
                    "utilization_percent_peak": None,
                    "utilization_percent_mean": None,
                    "utilization_sample_count": 0,
                    "vram_used_bytes_peak": None,
                    "vram_total_bytes": None,
                },
            ],
        )

    def test_gpu_peaks_stay_with_their_logical_board(self):
        psutil = self.fake_psutil(
            native_samples=((100, 1, 0, 1), (100, 1, 0, 1)),
            host_samples=((10, 0, 0, 1), (10, 0, 0, 1)),
        )
        outputs = iter(
            (
                "0, 555, 10, 100, 1000\n1, 555, 90, 10, 2000\n",
                "0, 555, 20, 200, 1000\n1, 555, 30, 20, 2000\n",
            )
        )

        def nvidia_run(*args, **kwargs):
            return self.nvidia_run(next(outputs))(*args, **kwargs)

        with (
            patch("vntts.native_resources.psutil", psutil),
            patch("vntts.native_resources.subprocess.run", side_effect=nvidia_run),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            sampler._next_gpu_at = 0
            sampler._sample(3.0)
            summary = sampler.finish()

        self.assertEqual(summary["gpu"]["boards"][0]["utilization_percent_peak"], 20)
        self.assertEqual(summary["gpu"]["boards"][0]["utilization_percent_mean"], 15)
        self.assertEqual(summary["gpu"]["boards"][0]["utilization_sample_count"], 2)
        self.assertEqual(
            summary["gpu"]["boards"][0]["vram_used_bytes_peak"], 200 * 1024 * 1024
        )
        self.assertEqual(summary["gpu"]["boards"][1]["utilization_percent_peak"], 90)
        self.assertEqual(summary["gpu"]["boards"][1]["utilization_percent_mean"], 60)
        self.assertEqual(summary["gpu"]["boards"][1]["utilization_sample_count"], 2)
        self.assertEqual(
            summary["gpu"]["boards"][1]["vram_used_bytes_peak"], 20 * 1024 * 1024
        )

    def test_gpu_rejects_unbounded_output_and_invalid_driver_or_vram(self):
        with (
            patch("vntts.native_resources.psutil", self.fake_psutil()),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run("x" * 8193),
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            oversized = sampler.finish()

        self.assertEqual(
            oversized["gpu"],
            {"status": "output-limit", "sample_count": 0, "boards": []},
        )

        with (
            patch("vntts.native_resources.psutil", self.fake_psutil()),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run("0, not-a-version, 10, 1e308, 1e308\n"),
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            invalid = sampler.finish()

        self.assertEqual(invalid["gpu"]["status"], "partial")
        board = invalid["gpu"]["boards"][0]
        self.assertIsNone(board["driver_version"])
        self.assertIsNone(board["vram_used_bytes_peak"])
        self.assertIsNone(board["vram_total_bytes"])

    def test_finish_snapshot_is_detached_from_later_board_updates(self):
        output = "0, 555, 10, 100, 1000\n"
        with (
            patch("vntts.native_resources.psutil", self.fake_psutil()),
            patch(
                "vntts.native_resources.subprocess.run",
                side_effect=self.nvidia_run(output),
            ),
        ):
            sampler = NativeResourceSampler(42)
            sampler._sample(1.0)
            summary = sampler.finish()

        sampler._gpu_boards[0]["utilization_percent_peak"] = 99
        self.assertEqual(summary["gpu"]["boards"][0]["utilization_percent_peak"], 10)

    def test_finish_returns_incomplete_snapshot_when_a_probe_is_stuck(self):
        entered = Event()
        release = Event()
        sampler = NativeResourceSampler(42)

        def blocked_memory():
            entered.set()
            release.wait()
            return SimpleNamespace(rss=1)

        process = SimpleNamespace(
            memory_info=blocked_memory,
            cpu_times=lambda: SimpleNamespace(user=0, system=0),
            num_threads=lambda: 1,
        )
        psutil = SimpleNamespace(
            Process=lambda _pid: process,
            NoSuchProcess=NoSuchProcess,
            AccessDenied=AccessDenied,
        )
        with patch("vntts.native_resources.psutil", psutil):
            sampler.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            summary = sampler.finish()
            elapsed = time.monotonic() - started
            release.set()
            sampler._worker.join(1)

        self.assertLess(elapsed, 0.3)
        self.assertFalse(summary["complete"])
        self.assertFalse(sampler._worker.is_alive())
