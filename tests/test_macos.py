import plistlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.macos import (
    MacOSLaunchAtLogin,
    configure_macos_launch_at_login,
    get_launch_arguments,
    get_macos_permission_status,
    launch_agent_label,
    request_accessibility_permission,
    request_screen_capture_permission,
)


class MacOSIntegrationTest(unittest.TestCase):
    def test_launch_arguments_support_source_and_packaged_app(self):
        executable = "/Applications/VNTTS.app/Contents/MacOS/VNTTS"

        with patch("vntts.macos.Path.resolve") as resolve:
            self.assertEqual(
                get_launch_arguments(executable=executable, frozen=True),
                [executable],
            )
        resolve.assert_not_called()
        self.assertEqual(
            get_launch_arguments(executable=executable, frozen=False),
            [executable, "-m", "vntts.app"],
        )

    def test_launch_agent_is_written_atomically_and_removed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "LaunchAgents" / "vntts.plist"
            logs = root / "logs"
            manager = MacOSLaunchAtLogin(
                path,
                arguments=["/Applications/VNTTS.app/Contents/MacOS/VNTTS"],
                log_directory=logs,
            )

            manager.configure(True)

            with path.open("rb") as source:
                payload = plistlib.load(source)
            self.assertTrue(manager.enabled)
            self.assertEqual(payload["Label"], launch_agent_label)
            self.assertTrue(payload["RunAtLoad"])
            self.assertFalse(payload["KeepAlive"])
            self.assertEqual(
                payload["ProgramArguments"],
                ["/Applications/VNTTS.app/Contents/MacOS/VNTTS"],
            )
            self.assertEqual(payload["StandardOutPath"], str(logs / "launch-agent.log"))

            manager.configure(False)

            self.assertFalse(manager.enabled)

    def test_platform_wrapper_only_changes_macos(self):
        manager = Mock()

        self.assertIsNone(
            configure_macos_launch_at_login(
                True,
                platform="linux",
                manager=manager,
            )
        )
        manager.configure.assert_not_called()

        configure_macos_launch_at_login(False, platform="darwin", manager=manager)

        manager.configure.assert_called_once_with(False)

    def test_permission_status_uses_native_probes(self):
        status = get_macos_permission_status(
            platform="darwin",
            screen_capture_probe=lambda: 0,
            accessibility_probe=lambda: 1,
        )

        self.assertEqual(
            status,
            {"screen_capture": False, "accessibility": True},
        )
        self.assertEqual(
            get_macos_permission_status(platform="linux"),
            {"screen_capture": None, "accessibility": None},
        )

    def test_permission_requests_delegate_to_native_apis(self):
        screen_request = Mock(return_value=1)
        accessibility_request = Mock(return_value=0)

        self.assertTrue(request_screen_capture_permission(screen_request))
        self.assertFalse(
            request_accessibility_permission(accessibility_request, "prompt")
        )

        screen_request.assert_called_once_with()
        accessibility_request.assert_called_once_with({"prompt": True})


if __name__ == "__main__":
    unittest.main()
