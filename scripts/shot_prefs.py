"""Model-preference screenshots with demo data only.

    .venv\\Scripts\\python scripts\\shot_prefs.py settings -> docs/screenshots/prefs-settings.png (rules table)
    .venv\\Scripts\\python scripts\\shot_prefs.py board    -> docs/screenshots/prefs-board.png (offer + Preference cards)
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
    which = sys.argv[1] if len(sys.argv) > 1 else "settings"
    out = ROOT / "docs" / "screenshots" / f"prefs-{which}.png"
    home = Path(tempfile.mkdtemp(prefix="orch-prefs-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core import preferences as P
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
    db.set_setting(P.CAPS_KEY, {"codex": True, "checked_at": time.time()})
    rules = P.rules(db)
    rules.append(P.Rule(name="Images", keywords=["logo", "illustration", "icon art"], engine="claude",
                        model="claude-sonnet-5-5", effort="medium", priority=4, notes="Marketing assets"))
    P.save_rules(db, rules)
    proj = db.add_project(r"D:\Demo\weather-app", "weather-app")
    now = time.time()
    if which == "board":
        goal = "Debug the flaky radar tile cache and tidy the README. I prefer Haiku 5.5 for docs work."
        o = db.add_orchestration(proj.id, goal, "claude", "claude-opus-5-5", "medium", 3)
        ps = db.create_session(proj.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · radar cache")
        db.update_orchestration(o.id, session_id=ps.id, created_at=now - 300,
                                pick_reason="Preference (planner): Complex coding → Opus 5.5 · medium (matched “debug”)")
        db.add_message(ps.id, "assistant", "text", {"text": "Delegated 3 tasks."})
        demo = [
            ("claude", "claude-opus-5-5", "medium", "Debug the radar tile cache race", "running", 140,
             "Preference: Complex coding → Opus 5.5 · medium (matched “debug”)", 1),
            ("claude", "claude-haiku-5-5", "low", "Tidy the README", "done", 200,
             "Preference: Quick edits and summaries → Haiku 5.5 · low (matched “readme”)", 1),
            ("codex", "gpt-6.2-sol", "medium", "Add a cache-hit metric", "running", 60,
             "Auto: GPT-6.2 Sol · medium — coding task (add)", 1),
        ]
        for engine, model, effort, title, status, st, why, auto in demo:
            t = db.add_task(o.id, engine, model, effort, title, title, proj.path)
            s = db.create_session(proj.id, engine, model, effort, parent_id=ps.id, title=title)
            db.add_message(s.id, "user", "text", {"text": title})
            db.update_task(t.id, session_id=s.id, status=status, started_at=now - st, pick_reason=why,
                           auto_picked=auto, finished_at=now - 20 if status == "done" else None)
        db.set_setting("orchestrate.project", proj.id)
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 900)
    win.show()

    def shoot():
        if which == "settings":
            win.switchTo(win.settings)
            page = win.settings.ensure()
            QTimer.singleShot(200, lambda: page.verticalScrollBar().setValue(
                page.rules.mapTo(page.widget(), page.rules.rect().topLeft()).y() - 120))
        else:
            win.switchTo(win.orchestrate)
            page = win.orchestrate.ensure()
            om = win._ensure_om()
            om.offer_rule(o.id, P.parse_preference(db, goal))
            page.reload()
            page.current = o.id
            page.render_board()
        QTimer.singleShot(900, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
