"""Side-by-side screenshot: orchestrator chat on the left, one agent's chat on the right (demo data only).

    .venv\\Scripts\\python scripts\\shot_split.py  -> docs/screenshots/split-view.png
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
    out = ROOT / "docs" / "screenshots" / "split-view.png"
    home = Path(tempfile.mkdtemp(prefix="orch-shot-"))
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = Database()
    p = db.add_project(r"D:\Demo\weather-app", "weather-app")
    ps = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · 7-day forecast")
    db.add_message(ps.id, "user", "text", {"text": "Add a 7-day forecast endpoint with tests and docs"})
    db.add_message(ps.id, "assistant", "text", {"text": "I split this into three tasks:\n\n1. **Endpoint**: `GET /forecast?days=7` "
                   "(Codex · GPT-6.2 Sol)\n2. **Tests** for the endpoint (Claude · Sonnet 5.5)\n3. **README** section "
                   "(Claude · Haiku 5.5)\n\nWaiting for the endpoint before the tests start."})
    a = db.create_session(p.id, "codex", "gpt-6.2-sol", "medium", parent_id=ps.id, title="Add GET /forecast endpoint")
    db.add_message(a.id, "user", "text", {"text": "Add GET /forecast?days=7 to api/routes.py returning daily highs and lows."})
    db.add_message(a.id, "assistant", "text", {"text": "Added `forecast()` in `api/routes.py` and a `days` query "
                   "parameter (1 to 14, default 7). The handler reuses the hourly cache and groups by local date."})
    db.create_session(p.id, "claude", "claude-sonnet-5-5", "medium", parent_id=ps.id, title="Tests for /forecast")
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1500, 860)
    win.show()

    def shoot():
        win.open_task_chat(a.id)
        QTimer.singleShot(800, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
