"""Screenshot of the Projects home with demo data only.

    .venv\\Scripts\\python scripts\\shot_projects.py docs/screenshots/projects-home.png [--narrow]
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots/projects-home.png")
    narrow = "--narrow" in sys.argv
    home = Path(tempfile.mkdtemp(prefix="orch-proj-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS

    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 10))
    setFontFamilies(UI_FONTS)
    setTheme(Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    db = Database()
    now = time.time()
    demo = [  # name, pinned, icon, color, last activity
        ("weather-app", True, "CLOUD", "#38bdf8", now - 60),
        ("api-server", True, "CODE", "#4ade80", now - 3 * 3600),
        ("design-system", True, "PALETTE", "#f472b6", now - 2 * 86400),
        ("game-jam-2026", False, None, None, now - 5 * 60),
        ("docs-site", False, "BOOK_SHELF", "#facc15", now - 26 * 3600),
        ("home-automation", False, "IOT", "#fb923c", now - 4 * 86400),
        ("ml-experiments", False, "ROBOT", "#a78bfa", datetime(2026, 11, 10, 15, 0).timestamp()),
        ("photo-tools", False, None, None, datetime(2026, 10, 28, 9, 0).timestamp()),
    ]
    for name, pinned, icon, color, last in demo:
        p = db.add_project(rf"D:\Demo\{name}", name)
        db.update_project(p.id, pinned=pinned, icon=icon, color=color)
        s = db.create_session(p.id, "codex", "gpt-6.2-sol", "medium")
        db.update_session(s.id, last_active=last, title="demo")
    db.add_project(r"D:\Demo\old-prototype", "old-prototype", auto=True)

    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(800 if narrow else 1320, 860)
    win.show()

    def shoot():
        win.projects.reload()
        QTimer.singleShot(400, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(600, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
