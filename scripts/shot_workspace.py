"""Orchestrate workspace screenshots (demo data only): planner chat on the left, board or an agent's chat right.

    .venv\\Scripts\\python scripts\\shot_workspace.py board  -> docs/screenshots/orch-workspace.png
    .venv\\Scripts\\python scripts\\shot_workspace.py agent  -> docs/screenshots/orch-workspace-agent.png
    .venv\\Scripts\\python scripts\\shot_workspace.py home   -> docs/screenshots/orch-home.png
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
    which = sys.argv[1] if len(sys.argv) > 1 else "board"
    out = Path(os.environ["SHOT_OUT"]) if os.environ.get("SHOT_OUT") else ROOT / "docs" / "screenshots" / {"board": "orch-workspace.png", "agent": "orch-workspace-agent.png",
                                           "home": "orch-home.png"}[which]
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
    asyncio.set_event_loop(asyncio.new_event_loop())
    db = Database()
    now = time.time()
    proj = db.add_project(r"D:\Demo\weather-app", "weather-app")
    goal = "Add a 7-day forecast endpoint with tests and docs"
    o = db.add_orchestration(proj.id, goal, "claude", "claude-opus-5-5", "medium", 2)
    ps = db.create_session(proj.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · 7-day forecast")
    db.update_orchestration(o.id, session_id=ps.id, created_at=now - 240)
    db.add_message(ps.id, "user", "text", {"text": goal})
    db.add_message(ps.id, "assistant", "text", {"text": "I split this into three tasks: the endpoint (Codex), tests "
                   "(Sonnet) and a README section (Haiku). The endpoint is running; tests wait for it."})
    db.add_message(ps.id, "user", "text", {"text": "Also cap days at 14 and return 400 above that."})
    db.add_message(ps.id, "assistant", "text", {"text": "Sent that to the endpoint task as a follow-up. "
                   "I'll check its report when it finishes."})
    agent = None
    for i, (engine, model, effort, title, status, st) in enumerate([
            ("codex", "gpt-6.2-sol", "medium", "Add GET /forecast?days=7 endpoint", "running", 150),
            ("claude", "claude-sonnet-5-5", "medium", "Write tests for the forecast endpoint", "queued", None),
            ("claude", "claude-haiku-5-5", "low", "Document the endpoint in README", "queued", None)]):
        t = db.add_task(o.id, engine, model, effort, title, title, proj.path)
        s = db.create_session(proj.id, engine, model, effort, parent_id=ps.id, title=title)
        db.add_message(s.id, "user", "text", {"text": title})
        if i == 0:
            agent = s.id
            db.add_message(s.id, "assistant", "text", {"text": "Added `forecast()` in `api/routes.py` with a `days` "
                           "parameter (1 to 14, default 7); above 14 returns 400."})
        db.update_task(t.id, session_id=s.id, status=status, started_at=now - st if st else None, queue_pos=now + i)
    for goal2, st2, ago in (("Fix flaky radar tile cache", "running", 3600), ("Upgrade to Python 3.13", "done", 86400)):
        o2 = db.add_orchestration(proj.id, goal2, "claude", "claude-opus-5-5", "medium", 2)
        db.update_orchestration(o2.id, status=st2, created_at=now - ago, finished_at=now - ago + 900)
        if st2 == "running":
            db.add_question(o2.id, "Clear the cache on deploy, or only on version change?", ["On deploy", "On version change"])
        for k in range(4 if st2 == "done" else 0):
            t2 = db.add_task(o2.id, "claude", "claude-sonnet-5-5", "low", f"step {k}", f"step {k}", proj.path)
            db.update_task(t2.id, status="done")
    db.add_project(r"D:\Games\sparkfall", "sparkfall")
    db.set_setting("orchestrate.project", proj.id)
    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    if os.environ.get("SHOT_THEME"):  # e.g. SHOT_THEME=Black
        from orchestrator.ui import appearance as A

        A.apply(A.Look(theme=os.environ["SHOT_THEME"]))
    win.resize(1500, 880)
    win.show()

    def shoot():
        win.switchTo(win.orchestrate)
        ws = win.orchestrate.ensure()
        ws.reload()
        if which != "home":
            ws.history.setCurrentIndex(max(0, ws.history.findData(o.id)))
        if which == "agent":
            win.open_task_chat(agent)
        QTimer.singleShot(700, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
