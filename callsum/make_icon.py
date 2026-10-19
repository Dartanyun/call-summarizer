"""Иконка в форматах для ярлыков: python -m callsum.make_icon <файл.ico|.png> [размер]"""

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def render(out: Path, size: int = 256) -> None:
    renderer = QSvgRenderer(str(Path(__file__).parent / "icon.svg"))
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(out)):
        raise SystemExit(f"Не удалось сохранить {out}")


if __name__ == "__main__":
    app = QGuiApplication(sys.argv[:1])
    render(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 256)
