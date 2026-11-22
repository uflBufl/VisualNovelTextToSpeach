"""Compare a known grey ramp with the Windows capture worker and its MP4 output."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import mkdtemp

from PIL import Image, ImageStat
from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from vntts.native_capture import NativeCaptureSession, worker_executable

LEVELS = (0, 32, 64, 96, 128, 160, 192, 224, 255)


class GreyRamp(QWidget):
    def paintEvent(self, event: QPaintEvent) -> None:
        with QPainter(self) as painter:
            for i, grey in enumerate(LEVELS):
                x = i * self.width() // len(LEVELS)
                right = (i + 1) * self.width() // len(LEVELS)
                painter.fillRect(
                    x, 0, right - x, self.height(), QColor(grey, grey, grey)
                )


def sample_ramp(
    image: Image.Image, content: tuple[int, int, int, int]
) -> list[list[int]]:
    left, top, width, height = content
    y = top + height // 2
    samples = []
    for i in range(len(LEVELS)):
        x = left + (2 * i + 1) * width // (2 * len(LEVELS))
        samples.append(ImageStat.Stat(image.crop((x - 2, y - 2, x + 3, y + 3))).median)
    return samples


def measure(hwnd: int, display: str, output: Path) -> None:
    report: dict[str, object] = {
        "os_version": platform.version(),
        "display": display,
        "expected_gray": LEVELS,
    }
    try:
        worker = worker_executable()
        with worker.open("rb") as binary:
            report["worker_sha256"] = hashlib.file_digest(binary, "sha256").hexdigest()
        pid = os.getpid()
        with NativeCaptureSession(
            {"kind": "window", "window_id": hwnd, "pid": pid}, command=[str(worker)]
        ) as session:
            frame = session.capture()
        report["capture_rgb"] = sample_ramp(frame.to_image(), frame.content_rect)
        # Use the same static window and production encoder. Its stdin must stay
        # open until the requested duration expires; EOF means Stop.
        recording = output / "recording"
        with subprocess.Popen(
            [str(worker), "--video-only", str(pid), str(hwnd), "3", str(recording)],
            stdin=subprocess.PIPE,
        ) as recorder:
            try:
                status = recorder.wait(timeout=30)
            finally:
                if recorder.poll() is None:
                    recorder.kill()
        report["recorder_exit_code"] = status
        import cv2

        video = cv2.VideoCapture(str(recording / "game.mp4"))
        try:
            ok, bgr = video.read()
            if not ok:
                raise RuntimeError("The recorded MP4 has no decodable frame.")
            rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            # The fixed window has the same surface in both captures. The
            # writer rounds odd extents down to even and fits the source into it.
            if rgb.size != (frame.width & ~1, frame.height & ~1):
                raise RuntimeError("The window changed size during measurement. Retry.")
            report["decoded_mp4_rgb"] = sample_ramp(rgb, frame.content_rect)
        finally:
            video.release()
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        path = output / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Colour report: {path}", flush=True)


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("This probe uses Windows Graphics Capture.")
    app = QApplication(sys.argv[:1])
    chart = GreyRamp()
    chart.setWindowTitle("Move this grey ramp to the game's display")
    chart.setFixedSize(540, 240)
    downloads = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
    )
    downloads.mkdir(parents=True, exist_ok=True)
    button = QPushButton("Measure on this display", chart)
    button.adjustSize()
    button.move((chart.width() - button.width()) // 2, 18)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending: Future[None] | None = None
        timer = QTimer(chart)

        def start() -> None:
            nonlocal pending
            chart.setWindowTitle("Measuring capture and recording…")
            button.hide()
            chart.repaint()
            output = Path(mkdtemp(prefix="sdr-check-", dir=downloads))
            pending = pool.submit(
                measure, int(chart.winId()), chart.screen().name(), output
            )
            timer.start(100)

        def check_finished() -> None:
            if pending is not None and pending.done():
                app.quit()

        timer.timeout.connect(check_finished)
        button.clicked.connect(start)
        chart.show()
        app.exec()
        if pending is not None:
            pending.result()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
