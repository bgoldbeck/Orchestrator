"""Screenshot of the Orchestrate board with demo data only (no engines run).

    .venv\\Scripts\\python scripts\\shot_m4.py docs/screenshots/m4.png [--chats]
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots/m4.png")
    chats = "--chats" in sys.argv
    home = Path(tempfile.mkdtemp(prefix="orch-m4-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core.events import NeedsApproval
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
    proj = db.add_project(r"D:\Demo\weather-app", "weather-app")
    now = time.time()
    goal = "Add a 7-day forecast endpoint with tests, and document it in the README"
    o = db.add_orchestration(proj.id, goal, "claude", "claude-opus-5-5", "high", 3)
    ps = db.create_session(proj.id, "claude", "claude-opus-5-5", "high", title="Orchestrate · Add a 7-day forecast")
    db.update_orchestration(o.id, session_id=ps.id, created_at=now - 312)
    db.add_message(ps.id, "user", "text", {"text": goal})
    db.add_message(ps.id, "assistant", "text", {"text": "Split into 4 tasks: API endpoint, tests, README, and a "
                                                        "client helper. Waiting for the first reports."})
    demo = [  # engine, model, effort, title, status, started ago, finished ago, result
        ("codex", "gpt-6.2-sol", "medium", "Add GET /forecast?days=7 endpoint", "done", 300, 120,
         {"status": "done", "summary": "Added the /forecast route with a days parameter (1-14) and caching.",
          "files_changed": ["api/routes/forecast.py", "api/app.py"], "open_questions": []}),
        ("claude", "claude-sonnet-5-5", "medium", "Write tests for the forecast endpoint", "running", 110, None, None),
        ("claude", "claude-haiku-5-5", "low", "Document the endpoint in README", "waiting", 95, None, None),
        ("codex", "gpt-6-luna", "low", "Add forecast() to the JS client", "queued", None, None, None),
        ("codex", "gpt-6.2-astra", "high", "Check rate limits for the weather API", "failed", 280, 200,
         {"status": "failed", "summary": "The upstream API key is not set in this environment, so limits could "
          "not be checked.", "files_changed": [], "open_questions": ["Which API key should staging use?"]}),
    ]
    waiting_sid = None
    for engine, model, effort, title, status, st, fin, res in demo:
        t = db.add_task(o.id, engine, model, effort, title, title, proj.path)
        s = db.create_session(proj.id, engine, model, effort, parent_id=ps.id, title=title)
        db.update_session(s.id, status={"queued": "idle", "failed": "error"}.get(status, status))
        db.add_message(s.id, "user", "text", {"text": title})
        if status == "running":
            db.add_message(s.id, "tool", "tool", {"name": "Bash", "summary": "pytest tests/test_forecast.py -q",
                                                  "output": "", "is_error": False})
        if status == "waiting":
            waiting_sid = s.id
        db.update_task(t.id, session_id=s.id, status=status, result=res,
                       started_at=now - st if st else None, finished_at=now - fin if fin else None)

    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 900)
    win.show()
    fut = loop.create_future()
    mgr.approval_requested.emit(waiting_sid, NeedsApproval("p1", "Edit", "README.md"), fut)

    def shoot():
        if chats:
            win.open_session(waiting_sid)
        else:
            win.switchTo(win.orchestrate)
            win.orchestrate.reload()
        QTimer.singleShot(600, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
