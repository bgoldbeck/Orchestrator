"""Token breakdown dialog screenshot with demo data only.

    .venv\\Scripts\\python scripts\\shot_context.py  -> docs/screenshots/context-breakdown.png
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_USAGE = {
    "categories": [{"name": "System prompt", "tokens": 3100}, {"name": "System tools", "tokens": 11800},
                   {"name": "MCP tools", "tokens": 6400}, {"name": "Memory files", "tokens": 420},
                   {"name": "Skills", "tokens": 7600}, {"name": "Messages", "tokens": 18200},
                   {"name": "Free space", "tokens": 152480}],
    "totalTokens": 47520, "maxTokens": 200000, "model": "claude-opus-5-5",
    "memoryFiles": [{"path": "~/.claude/CLAUDE.md", "tokens": 95, "type": "User"},
                    {"path": "D:/Demo/weather-app/CLAUDE.md", "tokens": 325, "type": "Project"}],
    "mcpTools": [{"name": f"t{i}", "serverName": "orchestrator", "tokens": 260} for i in range(8)]
    + [{"name": f"s{i}", "serverName": "spelldirector", "tokens": 420} for i in range(10)],
}
DEMO_INIT = {"mcp_servers": [{"name": "orchestrator", "status": "connected"},
                             {"name": "spelldirector", "status": "connected"},
                             {"name": "midimcp", "status": "failed"}],
             "plugins": [{"name": n} for n in ("superpowers", "caveman", "unity", "twilio-developer-kit",
                                               "small-business", "canva")],
             "skills": ["x"] * 214, "tools": ["t"] * 40}


def main() -> int:
    out = ROOT / "docs" / "screenshots" / "context-breakdown.png"
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
    from orchestrator.engines.claude import summarize_context
    from orchestrator.storage.db import Database
    from orchestrator.ui.context_view import ContextDialog
    from orchestrator.ui.main_window import MainWindow
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS

    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 10))
    setFontFamilies(UI_FONTS)
    setTheme(Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    asyncio.set_event_loop(asyncio.new_event_loop())
    db = Database()
    p = db.add_project(r"D:\Demo\weather-app", "weather-app")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Forecast cache")
    db.add_message(s.id, "user", "text", {"text": "Why is the forecast cache missing?"})
    db.add_message(s.id, "assistant", "text", {"text": "The cache key ignores the units setting."})
    db.add_message(s.id, "system", "usage", {"input": 1840, "output": 612, "cached": 45200, "cost": 0.03})
    db.set_context(s.id, summarize_context(DEMO_USAGE, DEMO_INIT))
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 900)
    win.show()

    def shoot():
        win.open_session(s.id)
        dlg = ContextDialog(db, mgr, s.id, win)
        dlg.show()
        QTimer.singleShot(700, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
