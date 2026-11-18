"""Screenshot of Settings › Models with the Enabled column (demo data only).

    .venv\\Scripts\\python scripts\\shot_models.py docs/screenshots/models-enabled.png
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots/models-enabled.png")
    home = Path(tempfile.mkdtemp(prefix="orch-models-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

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
    db.add_project(r"D:\Demo\weather-app", "weather-app")
    db.set_model_enabled("claude", "claude-fable-5-2", False)
    db.set_model_enabled("codex", "gpt-6.1-terra", False)
    db.set_model_enabled("codex", "gpt-6-luna", False)

    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 900)
    win.show()

    def shoot():
        win.switchTo(win.settings)
        win.settings.ensureWidgetVisible(win.settings.models, 0, 0)
        bar = win.settings.verticalScrollBar()
        bar.setValue(win.settings.models.y() - 80)
        QTimer.singleShot(500, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
