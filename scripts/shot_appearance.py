"""Appearance screenshots with demo data only.

    .venv\\Scripts\\python scripts\\shot_appearance.py dark  -> docs/screenshots/appearance-dark.png (defaults)
    .venv\\Scripts\\python scripts\\shot_appearance.py light -> docs/screenshots/appearance-light.png
        (Light, Violet, UI 11 pt, chat 12 pt, Consolas 10.5 pt, Compact)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "dark"
    out = ROOT / "docs" / "screenshots" / f"appearance-{which}.png"
    home = Path(tempfile.mkdtemp(prefix="orch-look-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from orchestrator.app import _app_icon
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui import appearance as A
    from orchestrator.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = Database()
    if which == "light":
        A.save(db, A.Look(ui_size=11.0, chat_size=12.0, code_font="Consolas", code_size=10.5, density="Compact",
                          theme="Light", accent="Violet"))
    A.apply(A.load(db), emit=False)
    db.add_project(r"D:\Demo\weather-app", "weather-app")
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 900)
    win.show()

    def shoot():
        win.switchTo(win.settings)
        page = win.settings.ensure()
        QTimer.singleShot(250, lambda: page.verticalScrollBar().setValue(
            page.appearance.mapTo(page.widget(), page.appearance.rect().topLeft()).y() - 110))
        QTimer.singleShot(1000, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
