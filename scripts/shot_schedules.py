"""Schedules screenshots with demo data only.

    .venv\\Scripts\\python scripts\\shot_schedules.py page     -> docs/screenshots/schedules-page.png
    .venv\\Scripts\\python scripts\\shot_schedules.py narrow   -> docs/screenshots/schedules-narrow.png
    .venv\\Scripts\\python scripts\\shot_schedules.py once     -> docs/screenshots/schedules-once.png
    .venv\\Scripts\\python scripts\\shot_schedules.py dialog   -> docs/screenshots/schedules-dialog.png
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "page"
    out = ROOT / "docs" / "screenshots" / f"schedules-{which}.png"
    home = Path(tempfile.mkdtemp(prefix="orch-sched-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    import asyncio

    import qasync
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS

    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 10))
    setFontFamilies(UI_FONTS)
    setTheme(Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    db = Database()
    proj = db.add_project(r"D:\Demo\weather-app", "weather-app")
    base = dict(project_id=proj.id, new_or_resume="new", permission="read-only", missed="run_once", enabled=True)
    db.save_schedule({**base, "name": "Morning repo digest", "prompt": "Summarize yesterday's commits and open PRs.",
                      "engine": "codex", "model": "gpt-6.2-sol", "effort": "low", "kind": "cron",
                      "cron": "0 8 * * mon-fri"})
    db.save_schedule({**base, "name": "Dependency check", "prompt": "List outdated dependencies and risky upgrades.",
                      "engine": "claude", "model": "claude-sonnet-5-5", "effort": "medium", "kind": "once",
                      "run_at": time.time() + 5 * 3600, "permission": "allow-edits"})
    mgr = SessionManager(db)
    runner = ScheduleRunner(db, mgr, f"sqlite:///{(home / 'jobs.db').as_posix()}")
    win = MainWindow(db, mgr, _app_icon(), runner=runner)
    win.setMicaEffectEnabled(False)
    win.resize(860 if which == "narrow" else 1320, 860)
    win.show()

    def shoot():
        win.switchTo(win.schedules)
        page = win.schedules.ensure()
        page.reload()
        if which in ("once", "custom"):
            page.run_once({"prompt": "Run the full test suite and summarize failures"}, page.onceBtn)
            if which == "custom":
                from orchestrator.ui.run_once import RunOnceView

                view = next(w for w in app.topLevelWidgets() for w in w.findChildren(RunOnceView))
                view.picker.select("Custom")
        elif which == "dialog":
            from orchestrator.ui.schedule_dialog import ScheduleDialog

            dlg = ScheduleDialog(db, win)
            dlg.when.setCurrentItem("once")
            dlg._update_when()
            dlg.name.setText("Nightly test run")
            dlg.prompt.setPlainText("Run the full test suite and summarize failures")
            dlg.show()
        def grab():
            g = win.frameGeometry()  # screen grab so popups (flyouts) are included
            app.primaryScreen().grabWindow(0, g.x(), g.y(), g.width(), g.height()).save(str(out))
            print("screenshot:", out)
            app.quit()

        QTimer.singleShot(900, grab)

    QTimer.singleShot(700, shoot)
    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
