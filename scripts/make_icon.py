"""Write build/orchestrator.ico from the app's drawn icon (used by the PyInstaller build)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from orchestrator.app import _app_icon

    app = QApplication(sys.argv)  # noqa: F841
    out = ROOT / "build" / "orchestrator.ico"
    out.parent.mkdir(exist_ok=True)
    ok = _app_icon().pixmap(64, 64).save(str(out), "ICO")
    print("icon:", out if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
