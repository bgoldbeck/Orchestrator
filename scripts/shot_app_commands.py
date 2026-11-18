"""App commands in a chat, with demo data only.

    .venv\\Scripts\\python scripts\\shot_app_commands.py -> docs/screenshots/app-commands.png
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
    out = ROOT / "docs" / "screenshots" / "app-commands.png"
    home = Path(tempfile.mkdtemp(prefix="orch-appcmd-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    import qasync
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from orchestrator.app import _app_icon
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui import appearance as A
    from orchestrator.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    db = Database()
    A.apply(A.load(db), emit=False)
    proj = db.add_project(r"D:\Demo\weather-app", "weather-app")
    sid = db.create_session(proj.id, "claude", "claude-sonnet-5-5", "medium", title="App settings").id
    db.set_setting("chats.last_session", sid)
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 860)
    win.show()
    ctl = win.ensure_app_control()

    def say(text: str) -> None:
        db.add_message(sid, "user", "text", {"text": text})

    def answer(text: str) -> None:
        db.add_message(sid, "assistant", "text", {"text": text})

    async def script() -> None:
        win.switchTo(win.chats)
        chat = win.chats.ensure().chat
        say("prefer design and planning to be opus 5.5 - medium, and coding should be gpt 6.2 sol medium")
        await ctl.handle(sid, "set_model_preference", {"task_type": "Design and planning", "model": "opus 5.5",
                                                       "effort": "medium"})
        await ctl.handle(sid, "set_model_preference", {"task_type": "Coding", "model": "gpt 6.2 sol",
                                                       "effort": "medium"})
        answer("Done: two preference rules. Design and planning go to Opus 5.5 (medium), coding to GPT-6.2 Sol "
               "(medium). You can undo either one below.")
        say("make a run once for x_y_z tomorrow 8am")
        await ctl.handle(sid, "create_schedule", {"prompt": "Run x_y_z and summarize the result",
                                                  "when": "tomorrow 8am", "name": "x_y_z"})
        answer("Scheduled. It runs tomorrow at 8 AM in weather-app with this chat's model.")
        win.chats.sidebar.reload()
        chat.show_session(sid)
        chat._send("increase the font size")  # local fast path: no model call
        fut = asyncio.get_running_loop().create_future()
        say("delete the x_y_z schedule")
        chat.show_session(sid)
        chat.show_confirm("Delete the schedule “x_y_z”?", fut)
        QTimer.singleShot(900, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(800, lambda: asyncio.ensure_future(script()))
    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
