import plistlib
import sys
from collections.abc import Callable, Sequence
from os import PathLike
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict

from vntts_artifacts.atomic_io import atomic_output_path

from vntts.settings import get_local_data_directory

launch_agent_label = "io.github.visualnoveltexttospeech.login"
PathInput: TypeAlias = str | PathLike[str]
PermissionProbe: TypeAlias = Callable[[], object]
PermissionRequest: TypeAlias = Callable[[], object]
AccessibilityPermissionRequest: TypeAlias = Callable[[dict[object, bool]], object]


class MacOSPermissionStatus(TypedDict):
    screen_capture: bool | None
    accessibility: bool | None


class LaunchAtLoginManager(Protocol):
    def configure(self, enabled: bool) -> Path: ...


def get_launch_agent_path(home: PathInput | None = None) -> Path:
    home = Path.home() if home is None else Path(home)
    return home / "Library" / "LaunchAgents" / f"{launch_agent_label}.plist"


def get_launch_arguments(
    *, executable: PathInput | None = None, frozen: bool | None = None
) -> list[str]:
    executable = (
        str(Path(sys.executable).resolve()) if executable is None else str(executable)
    )
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    return [executable] if frozen else [executable, "-m", "vntts.app"]


class MacOSLaunchAtLogin:
    def __init__(
        self,
        path: PathInput | None = None,
        *,
        arguments: Sequence[str] | None = None,
        log_directory: PathInput | None = None,
    ) -> None:
        self.path = get_launch_agent_path() if path is None else Path(path)
        self.arguments = (
            list(arguments) if arguments is not None else get_launch_arguments()
        )
        self.log_directory = (
            get_local_data_directory() / "logs"
            if log_directory is None
            else Path(log_directory)
        )

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def configure(self, enabled: bool) -> Path:
        if not enabled:
            self.path.unlink(missing_ok=True)
            return self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": launch_agent_label,
            "ProgramArguments": list(self.arguments),
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "StandardOutPath": str(self.log_directory / "launch-agent.log"),
            "StandardErrorPath": str(self.log_directory / "launch-agent-error.log"),
        }
        with atomic_output_path(self.path) as temporary_path:
            with temporary_path.open("wb") as output:
                plistlib.dump(payload, output)
        return self.path


def configure_macos_launch_at_login(
    enabled: bool,
    *,
    platform: str | None = None,
    manager: LaunchAtLoginManager | None = None,
) -> Path | None:
    if (platform or sys.platform) != "darwin":
        return None
    return (manager or MacOSLaunchAtLogin()).configure(enabled)


def get_macos_permission_status(
    *,
    platform: str | None = None,
    screen_capture_probe: PermissionProbe | None = None,
    accessibility_probe: PermissionProbe | None = None,
) -> MacOSPermissionStatus:
    if (platform or sys.platform) != "darwin":
        return {"screen_capture": None, "accessibility": None}
    if screen_capture_probe is None:
        try:
            from Quartz import CGPreflightScreenCaptureAccess

            screen_capture_probe = CGPreflightScreenCaptureAccess
        except AttributeError, ImportError:
            screen_capture_probe = None
    if accessibility_probe is None:
        try:
            from ApplicationServices import AXIsProcessTrusted

            accessibility_probe = AXIsProcessTrusted
        except AttributeError, ImportError:
            accessibility_probe = None
    return {
        "screen_capture": (
            bool(screen_capture_probe()) if screen_capture_probe is not None else None
        ),
        "accessibility": (
            bool(accessibility_probe()) if accessibility_probe is not None else None
        ),
    }


def request_screen_capture_permission(request: PermissionRequest | None = None) -> bool:
    if request is None:
        from Quartz import CGRequestScreenCaptureAccess

        request = CGRequestScreenCaptureAccess
    return bool(request())


def request_accessibility_permission(
    request: AccessibilityPermissionRequest | None = None,
    prompt_option: object | None = None,
) -> bool:
    if request is None or prompt_option is None:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        request = request or AXIsProcessTrustedWithOptions
        prompt_option = prompt_option or kAXTrustedCheckOptionPrompt
    return bool(request({prompt_option: True}))
