"""Icon sheet: every size on a dark and a light strip, plus 16/24 px magnified (pixel legibility).

    .venv\\Scripts\\python scripts\\shot_icon.py -> docs/screenshots/app-icon.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))



def main() -> int:
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.app_icon import icon_file

    app = QApplication(sys.argv)  # noqa: F841
    sizes = [16, 24, 32, 48, 64, 128, 256]
    W, H = 1100, 760
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor("#202020"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont("Segoe UI", 11)
    p.setFont(f)
    for row, (bg, fg) in enumerate((("#202020", "#c8c8c8"), ("#f3f3f3", "#333333"))):
        top = 20 + row * 330
        p.fillRect(0, top - 20, W, 330, QColor(bg))
        x = 30
        for s in sizes:
            pm = QPixmap(str(icon_file(s)))
            p.drawPixmap(x, top + 270 - s, pm)
            p.setPen(QColor(fg))
            p.drawText(QRect(x - 10, top + 278, s + 20, 24), Qt.AlignHCenter, f"{s}")
            x += s + 40
    # magnified 16 and 24 (nearest neighbour) on both backgrounds
    x0 = 30 + sum(sizes[:6]) + 6 * 40 + 20
    for row, bg in enumerate(("#202020", "#f3f3f3")):
        top = 20 + row * 330
        for i, s in enumerate((16, 24)):
            pm = QPixmap(str(icon_file(s))).scaled(s * 4, s * 4, Qt.KeepAspectRatio, Qt.FastTransformation)
            p.drawPixmap(x0 + 280 + i * 120, top + 20, pm)
            p.setPen(QColor("#c8c8c8" if row == 0 else "#333333"))
            p.drawText(QRect(x0 + 280 + i * 120, top + 20 + s * 4 + 4, s * 4, 24), Qt.AlignHCenter, f"{s} px ×4")
    p.end()
    out = ROOT / "docs" / "screenshots" / "app-icon.png"
    img.save(str(out))
    print("screenshot:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
