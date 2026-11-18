"""Orchestrate page screenshots with demo data only (no engines run).

    .venv\\Scripts\\python scripts\\shot_orch.py composer  -> docs/screenshots/orch-composer.png
    .venv\\Scripts\\python scripts\\shot_orch.py queue     -> docs/screenshots/orch-queue.png
    .venv\\Scripts\\python scripts\\shot_orch.py empty     -> docs/screenshots/orch-empty.png
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
    which = sys.argv[1] if len(sys.argv) > 1 else "composer"
    out = ROOT / "docs" / "screenshots" / f"orch-{which}.png"
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
    now = time.time()
    projects = {}
    if which != "empty":
        for name in ("weather-app", "billing-service", "docs-site"):
            projects[name] = db.add_project(rf"D:\Demo\{name}", name)
        s = db.create_session(projects["billing-service"].id, "codex", "gpt-6-luna", "low", title="Stripe invoices")
        db.update_session(s.id, summary="Invoice PDF export and Stripe webhooks", keywords="invoice, stripe, webhook")
    if which == "queue":
        proj = projects["weather-app"]
        goal = "Add a 7-day forecast endpoint with tests, client helper and docs"
        o = db.add_orchestration(proj.id, goal, "claude", "claude-opus-5-5", "medium", 2)
        ps = db.create_session(proj.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · 7-day forecast")
        db.update_orchestration(o.id, session_id=ps.id, created_at=now - 240,
                                project_reason="the goal names “weather”; recent chats mention forecast")
        db.add_message(ps.id, "assistant", "text", {"text": "Delegated 5 tasks; 2 run at once, the rest are queued."})
        demo = [
            ("codex", "gpt-6.2-sol", "medium", "Add GET /forecast?days=7 endpoint", "running", 150),
            ("claude", "claude-sonnet-5-5", "medium", "Write tests for the forecast endpoint", "running", 90),
            ("codex", "gpt-6-luna", "low", "Add forecast() to the JS client", "queued", None),
            ("claude", "claude-haiku-5-5", "low", "Document the endpoint in README", "queued", None),
            ("codex", "gpt-6-luna", "low", "Add a changelog entry", "queued", None),
        ]
        for i, (engine, model, effort, title, status, st) in enumerate(demo):
            t = db.add_task(o.id, engine, model, effort, title, title, proj.path)
            s = db.create_session(proj.id, engine, model, effort, parent_id=ps.id, title=title)
            db.update_session(s.id, status="running" if status == "running" else "idle")
            db.add_message(s.id, "user", "text", {"text": title})
            db.update_task(t.id, session_id=s.id, status=status, started_at=now - st if st else None,
                           queue_pos=now + i, pick_reason="Chosen by the planner")
        db.set_setting("orchestrate.project", proj.id)
    else:
        db.set_setting("orchestrate.project", "auto")
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 860)
    win.show()

    def shoot():
        win.switchTo(win.orchestrate)
        page = win.orchestrate.ensure()
        page.reload()
        if which == "composer":
            page.goal.setPlainText("Retry failed Stripe webhooks with exponential backoff and log the final failure")
            page._update_where()
        QTimer.singleShot(600, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
