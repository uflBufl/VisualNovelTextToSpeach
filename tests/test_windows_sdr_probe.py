import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageQt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from scripts.windows_sdr_probe import LEVELS, GreyRamp, sample_ramp  # noqa: E402


class WindowsSDRProbeTest(unittest.TestCase):
    def test_samples_chart_inside_native_window_frame(self):
        app = QApplication.instance() or QApplication([])
        chart = GreyRamp()
        chart.resize(541, 239)
        chart.show()
        self.addCleanup(chart.close)
        app.processEvents()
        client = ImageQt.fromqimage(chart.grab().toImage()).convert("RGB")
        surface = Image.new("RGB", (client.width + 14, client.height + 37), "red")
        surface.paste(client, (7, 30))
        actual = sample_ramp(surface, (7, 30, client.width, client.height))
        self.assertEqual(actual, [[grey] * 3 for grey in LEVELS])
