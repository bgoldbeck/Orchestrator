"""Waiting on you page screenshot with demo data only (no engines run).

    .venv\\Scripts\\python scripts\\shot_inbox.py [dark|light]  -> docs/screenshots/inbox-<theme>.png
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
    theme = sys.argv[1] if len(sys.argv) > 1 else "dark"
    out = ROOT / "docs" / "screenshots" / f"inbox-{theme}.png"
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
    from orchestrator.core import usage
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
    now = time.time()
    p = db.add_project(r"D:\Demo\billing-service", "billing-service")
    o = db.add_orchestration(p.id, "Annual plans with refunds", "claude", "claude-opus-5-5", "medium", 3)

    def task(title, status, **kw):
        t = db.add_task(o.id, "claude", "claude-sonnet-5-5", "medium", title, title, p.path)
        s = db.create_session(p.id, "claude", "claude-sonnet-5-5", "medium", title=title)
        db.update_task(t.id, session_id=s.id, status=status, **kw)
        return t.id

    db.add_question(o.id, "Should refunds go back to the card or become account credit?",
                    ["Account credit", "Back to the card"], "Back to the card")
    a = task("Refund flow for annual plans", "done", finished_at=now - 300, worktree=r"D:\wt\billing-task1",
             merge_state="review", checks={"ok": True, "sec": 41.2, "exit": 0, "cmd": "pytest -q", "tail": ""},
             result={"status": "done", "summary": "Added the refund endpoint, the pro-rata calculation and tests.",
                     "assumptions": ["pro-rata by day", "refunds over 30 days need support"],
                     "files_changed": ["billing/refunds.py"], "open_questions": []})
    task("Refund emails", "queued", depends_on=[a])
    b = task("Plans and prices table", "failed", finished_at=now - 900)
    task("Upgrade page", "queued", depends_on=[b],
         hold={"kind": "dep_failed", "task": b, "text": f"Waiting for you: its dependency T-{b} failed"})
    task("Stripe webhooks", "running", started_at=now - 34 * 60)
    db.set_setting(usage.CLAUDE_KEY, {"five_hour": {"status": "rejected", "resets_at": now + 2 * 3600}})
    db.add_task(o.id, "claude", "claude-haiku-5-5", "low", "Copy edits", "Copy edits", p.path)
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1300, 900)
    win.show()

    def shoot():
        win.switchTo(win.inbox)
        win.inbox.ensure().reload()
        win._update_inbox_count()
        QTimer.singleShot(600, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
