"""Board page screenshot with demo data only (no engines run).

    .venv\\Scripts\\python scripts\\shot_board.py [dark|light]  -> docs/screenshots/board-<theme>.png
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

DEMO = [  # project, engine, model, effort, title, status, minutes (running/elapsed or finished ago), reviewed
    ("weather-app", "codex", "gpt-6-luna", "low", "Fix typo in the onboarding text", "queued", 0, False),
    ("weather-app", "claude", "claude-haiku-5-5", "low", "Accessible labels on icon buttons", "queued", 0, False),
    ("docs-site", "claude", "claude-sonnet-5-5", "medium", "Empty states on all screens", "queued", 0, False),
    ("billing-service", "codex", "gpt-6.2-sol", "high", "Refund flow for annual plans", "proposed", 0, False),
    ("billing-service", "claude", "claude-opus-5-5", "medium", "Rate limit on the login endpoint", "running", 54, False),
    ("billing-service", "codex", "gpt-6.2-sol", "medium", "Stripe checkout", "running", 31, False),
    ("weather-app", "claude", "claude-sonnet-5-5", "medium", "Client sync queue with retry", "waiting", 12, False),
    ("docs-site", "codex", "gpt-6-luna", "low", "Password reset by email", "done", 4, False),
    ("weather-app", "claude", "claude-sonnet-5-5", "medium", "Forecast cache invalidation", "failed", 9, False),
    ("billing-service", "claude", "claude-opus-5-5", "high", "Plans and prices table", "done", 90, True),
    ("weather-app", "codex", "gpt-6.2-sol", "medium", "Change log table with per-device cursor", "done", 600, True),
    ("docs-site", "claude", "claude-haiku-5-5", "low", "Dark mode colours", "done", 1500, True),
]


def main() -> int:
    theme = sys.argv[1] if len(sys.argv) > 1 else "dark"
    out = ROOT / "docs" / "screenshots" / f"board-{theme}.png"
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
    setTheme(Theme.LIGHT if theme == "light" else Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = Database()
    db.set_setting("global.max_parallel", 5)
    now = time.time()
    projects = {n: db.add_project(rf"D:\Demo\{n}", n) for n in ("weather-app", "billing-service", "docs-site")}
    orchs = {}
    for i, (proj, engine, model, effort, title, status, mins, reviewed) in enumerate(DEMO):
        p = projects[proj]
        if proj not in orchs:
            orchs[proj] = db.add_orchestration(p.id, f"Demo goal for {proj}", "claude", "claude-opus-5-5", "medium", 3)
        t = db.add_task(orchs[proj].id, engine, model, effort, title, title, p.path)
        s = db.create_session(p.id, engine, model, effort, title=title)
        fields = dict(session_id=s.id, status=status, queue_pos=now + i)
        if status in ("running", "waiting"):
            fields["started_at"] = now - mins * 60
        elif status in ("done", "failed"):
            fields.update(started_at=now - mins * 60 - 1500, finished_at=now - mins * 60,
                          result={"status": "done" if status == "done" else "failed",
                                  "summary": "Added the endpoint and tests; all tests pass." if status == "done"
                                  else "Tests failed: cache key ignores the units setting.",
                                  "files_changed": ["api/forecast.py"], "open_questions": []})
            if reviewed:
                fields["reviewed_at"] = now - mins * 60 + 30
        elif status == "proposed":
            fields["proposal"] = {"kind": "pick"}
        db.update_task(t.id, **fields)
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1400, 860)
    win.show()

    def shoot():
        win.switchTo(win.board)
        win.board.ensure().reload()
        QTimer.singleShot(600, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
