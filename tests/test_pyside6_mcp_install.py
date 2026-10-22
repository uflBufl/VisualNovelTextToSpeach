"""Safety checks for the opt-in developer MCP installer."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_pyside6_mcp as installer


class Pyside6McpInstallTests(unittest.TestCase):
    def test_tracked_patch_is_present(self):
        self.assertTrue(installer.PATCH.is_file())
        self.assertIn(
            b"diff --git a/pyside6_mcp/bridge.py", installer.PATCH.read_bytes()
        )

    def test_existing_source_or_repository_is_never_touched(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            for existing in (source, source.with_name("source.git")):
                existing.mkdir()
                with patch.object(installer, "run") as run:
                    with self.assertRaises(ValueError):
                        installer.prepare_source(source)
                    run.assert_not_called()
                existing.rmdir()

    def test_patch_is_checked_before_application_in_isolated_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            with patch.object(installer, "run") as run:
                installer.prepare_source(source)
            calls = run.call_args_list
            self.assertIn("--bare", calls[0].args)
            self.assertIn("worktree", calls[1].args)
            self.assertEqual(calls[1].args[-1], installer.REVISION)
            self.assertEqual(
                calls[2].args, ("git", "apply", "--check", str(installer.PATCH))
            )
            self.assertEqual(calls[3].args, ("git", "apply", str(installer.PATCH)))

    def test_rejected_patch_is_never_applied(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(
                installer, "run", side_effect=["", "", RuntimeError("patch mismatch")]
            ) as run:
                with self.assertRaises(RuntimeError):
                    installer.prepare_source(Path(root) / "source")
            self.assertEqual(run.call_count, 3)
