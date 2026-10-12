"""Offline evidence checks must not initialize gameplay or native input stacks."""

import subprocess
import sys
import unittest
from pathlib import Path


class CoverageImportTest(unittest.TestCase):
    def test_coverage_cli_loads_without_gameplay_or_native_dependencies(self):
        code = """
import importlib.abc
import sys

blocked = (
    "vntts.live_replay_sequence_seal", "vntts.live_replay", "vntts.controller",
    "vntts.settings", "vntts.dialog_capture", "pynput", "PySide6", "sounddevice",
)
class OfflineGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Offline coverage initialized " + fullname)

sys.meta_path.insert(0, OfflineGuard())
from vntts.live_replay_coverage import build_parser
options = build_parser().parse_args([
    "coverage.json", "--story-index", "story.jsonl", "--sequence-plan", "plan.json",
    "--review", "review.json",
])
assert options.output.name == "coverage.json"
assert not any(name in sys.modules for name in blocked)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
