"""Render the app icon from assets/icon.svg (32 px and up), icon-small.svg (20/24 px) and icon-tiny.svg (16 px).

Writes orchestrator/assets/icons/icon-<size>.png (loaded at runtime), build/orchestrator.ico (multi-size, used by
PyInstaller and the installer) and orchestrator/assets/icons/orchestrator.ico (for shortcuts when run from source).

    .venv\\Scripts\\python scripts\\make_icon.py
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
OUT = ROOT / "orchestrator" / "assets" / "icons"


def render(svg: Path, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    QSvgRenderer(str(svg)).render(p)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data.data())


def write_ico(path: Path, pngs: dict[int, bytes]) -> None:
    """ICO with PNG-compressed entries (Windows Vista+), one per size."""
    sizes = sorted(pngs)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        blob = pngs[s]
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset + len(blobs))
        blobs += blob
    path.write_bytes(head + entries + blobs)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    OUT.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for s in sorted(set(SIZES) | set(ICO_SIZES)):
        svg = ROOT / "assets" / ("icon-tiny.svg" if s <= 16 else "icon-small.svg" if s <= 24 else "icon.svg")
        pngs[s] = render(svg, s)
        if s in SIZES:
            (OUT / f"icon-{s}.png").write_bytes(pngs[s])
    (ROOT / "build").mkdir(exist_ok=True)
    ico = {s: pngs[s] for s in ICO_SIZES}
    write_ico(ROOT / "build" / "orchestrator.ico", ico)
    write_ico(OUT / "orchestrator.ico", ico)
    print("icons:", OUT, "and build/orchestrator.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
