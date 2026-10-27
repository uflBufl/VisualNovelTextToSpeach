"""Explicit native capture qualification, with a synthetic occlusion fixture.

Examples (capture needs explicit human authorization):
  uv run python -m scripts.native_capture_probe --fixture
  uv run python -m scripts.native_capture_probe --window-id ID --pid PID \
      --allow-live --output capture-report
Add --save-frames only when the selected target's pixels may be saved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic
from typing import Any

from vntts.native_capture import CaptureError, NativeCaptureSession


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    target = result.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--fixture",
        action="store_true",
        help="Open our synthetic window and independent occluder; captures nothing",
    )
    target.add_argument(
        "--window-id",
        type=int,
        help="Explicit existing native window ID (HWND on Windows)",
    )
    target.add_argument(
        "--display-id", type=int, help="Explicit display ID; 0 selects primary display"
    )
    result.add_argument("--pid", type=int, help="Required owner PID for --window-id")
    result.add_argument(
        "--allow-live",
        action="store_true",
        help="Authorize starting native capture of the explicit target",
    )
    result.add_argument(
        "--output",
        type=Path,
        help="New report directory; contains metadata only by default",
    )
    result.add_argument(
        "--save-frames",
        action="store_true",
        help="Also authorize saving selected target pixels as PNG",
    )
    result.add_argument(
        "--frames", type=int, default=3, help="Bounded request count (1..100)"
    )
    result.add_argument(
        "--timeout-ms",
        type=int,
        default=2000,
        help="Per-frame native timeout (1..10000)",
    )
    return result


def capture_report(options: argparse.Namespace) -> dict[str, Any]:
    target = (
        {"kind": "window", "window_id": options.window_id, "pid": options.pid}
        if options.window_id is not None
        else {"kind": "display", "display_id": options.display_id}
    )
    options.output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "target": target,
        "python_clock": "monotonic; separate from worker clock",
        "frames": [],
    }
    try:
        with NativeCaptureSession(target) as session:
            report["backend"] = session.backend
            report["worker_clock_at_open"] = session.worker_clock_at_open
            for index in range(options.frames):
                started = monotonic()
                frame = session.capture(timeout_ms=options.timeout_ms)
                metadata = {
                    name: getattr(frame, name)
                    for name in frame.__dataclass_fields__
                    if name != "pixels"
                }
                metadata["python_request_started"] = started
                metadata["python_response_received"] = monotonic()
                report["frames"].append(metadata)
                if options.save_frames:
                    with (options.output / f"frame-{index + 1:03d}.png").open(
                        "xb"
                    ) as stream:
                        frame.to_image().save(stream, format="PNG")
    except CaptureError as error:
        report["error"] = {"code": error.code, "message": str(error)}
        raise
    finally:
        with (options.output / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
    return report


def fixture() -> int:
    """Only our own pixels; the independent window can obscure the target."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class Target(QWidget):
        stage = 0

        def paintEvent(self, event: Any) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#f5efdb"))
            size = 48
            for x, y, color in (
                (0, 0, "#ff0000"),
                (self.width() - size, 0, "#00ff00"),
                (0, self.height() - size, "#0000ff"),
                (self.width() - size, self.height() - size, "#ffff00"),
            ):
                painter.fillRect(x, y, size, size, QColor(color))
            painter.setPen(QColor("#111111"))
            painter.setFont(QFont("monospace", 26))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                f"VNTTS CAPTURE {self.stage}\nAa 0123 Ж文\nSYNTHETIC TARGET",
            )

    app = QApplication.instance() or QApplication(sys.argv[:1])
    target = Target()
    target.setWindowTitle("VNTTS native capture fixture - target")
    target.resize(640, 360)
    target.show()
    window_id = int(target.winId())
    if sys.platform == "darwin":
        import objc

        window_id = int(objc.objc_object(c_void_p=window_id).window().windowNumber())
    identity = {"window_id": window_id, "pid": os.getpid()}
    print(json.dumps(identity), flush=True)
    controller = QWidget()
    controller.setWindowTitle("VNTTS fixture - independent occluder and controls")
    layout = QVBoxLayout(controller)
    layout.addWidget(
        QLabel(
            f"Synthetic target: window_id={window_id}, pid={os.getpid()}\nMove this window over the target to test occlusion.\nNo capture runs in fixture mode."
        )
    )

    def next_stage() -> None:
        target.stage += 1
        target.update()

    def restore() -> None:
        target.showNormal()
        target.raise_()

    for text, callback in (
        ("Change target marker", next_stage),
        (
            "Resize target",
            lambda: target.resize(
                800 if target.width() == 640 else 640,
                450 if target.height() == 360 else 360,
            ),
        ),
        ("Fullscreen target", target.showFullScreen),
        ("Minimize target", target.showMinimized),
        ("Hide target", target.hide),
        ("Restore target", restore),
    ):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
    controller.resize(480, 320)
    controller.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    options = arguments.parse_args(argv)
    if options.fixture:
        if (
            options.allow_live
            or options.save_frames
            or options.output
            or options.pid is not None
        ):
            arguments.error(
                "--fixture only opens our synthetic UI; capture it with a separate explicit target command"
            )
        return fixture()
    if not options.allow_live:
        arguments.error("Live capture requires explicit --allow-live authorization")
    if options.output is None:
        arguments.error("Live capture requires --output for the metadata report")
    if not 1 <= options.frames <= 100 or not 1 <= options.timeout_ms <= 10000:
        arguments.error("--frames must be 1..100 and --timeout-ms must be 1..10000")
    if options.window_id is not None:
        if options.window_id <= 0 or options.pid is None or options.pid <= 0:
            arguments.error("--window-id requires a positive ID and --pid")
    elif options.pid is not None or options.display_id < 0:
        arguments.error("--display-id must be nonnegative and does not accept --pid")
    try:
        capture_report(options)
    except (CaptureError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Capture metadata: {options.output / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
