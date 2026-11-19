"""Attachments screenshot (demo data only): a sent message with files, and chips waiting in the message box.

    .venv\\Scripts\\python scripts\\shot_attachments.py  -> docs/screenshots/attachments.png
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
    out = ROOT / "docs" / "screenshots" / "attachments.png"
    home = Path(tempfile.mkdtemp(prefix="orch-shot-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QImage
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
    files = []
    for name in ("login-bug.png", "error.log"):
        f = home / name
        if name.endswith(".png"):
            QImage(8, 8, QImage.Format_RGB32).save(str(f))
        else:
            f.write_text("Traceback ...")
        files.append(str(f))
    db = Database()
    p = db.add_project(r"D:\Demo\weather-app", "weather-app")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Login button misaligned")
    db.add_message(s.id, "user", "text", {"text": "The login button is cut off on small screens. Screenshot attached.",
                                          "attachments": [files[0]]})
    db.add_message(s.id, "assistant", "text", {"text": "I can see it: the button sits outside the card below 380 px. "
                   "The form uses a fixed width; I'll switch it to max-width and let the button wrap."})
    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 820)
    win.show()

    def shoot():
        win.open_session(s.id)
        chat = win.chats.chat
        chat.prompt.edit.setPlainText("Here's the console output too")
        chat.prompt.add_attachments(files)
        QTimer.singleShot(700, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
