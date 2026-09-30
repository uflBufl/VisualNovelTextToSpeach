from PIL import Image
from PySide6.QtGui import QImage, QPixmap


def pixmap_from_pil(image: Image.Image) -> QPixmap:
    if image.mode != "RGB":
        image = image.convert("RGB")
    qimage = QImage(
        image.tobytes("raw", "RGB"),
        image.width,
        image.height,
        image.width * 3,
        QImage.Format.Format_RGB888,
    ).copy()
    return QPixmap.fromImage(qimage)
