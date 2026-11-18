"""Screenshots for M6 with demo data only.

    .venv\\Scripts\\python scripts\\shot_m6.py diff     -> docs/screenshots/m6-diff.png
    .venv\\Scripts\\python scripts\\shot_m6.py search   -> docs/screenshots/m6-search.png
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_repo(base: Path) -> Path:
    r = base / "weather-app"
    (r / "api" / "routes").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "demo@example.invalid")
    git("config", "user.name", "demo")
    (r / "api" / "app.py").write_text("from api.routes import current\n\n\ndef create_app():\n"
                                      "    app = App()\n    app.add(current.router)\n    return app\n")
    (r / "README.md").write_text("# weather-app\n\nCurrent conditions API.\n")
    git("add", ".")
    git("commit", "-qm", "init")
    (r / "api" / "app.py").write_text("from api.routes import current, forecast\n\n\ndef create_app():\n"
                                      "    app = App()\n    app.add(current.router)\n    app.add(forecast.router)\n"
                                      "    return app\n")
    (r / "api" / "routes" / "forecast.py").write_text(
        "from api.cache import cached\n\n\n@cached(minutes=10)\ndef forecast(city: str, days: int = 7):\n"
        "    if not 1 <= days <= 14:\n        raise ValueError(\"days must be 1-14\")\n"
        "    return provider.daily(city, days)\n")
    (r / "README.md").write_text("# weather-app\n\nCurrent conditions and 7-day forecast API.\n\n"
                                 "- `GET /forecast?city=Oslo&days=7`\n")
    return r


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "diff"
    out = ROOT / "docs" / "screenshots" / f"m6-{which}.png"
    home = Path(tempfile.mkdtemp(prefix="orch-m6-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"
    repo = make_repo(home)

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
    db = Database()
    proj = db.add_project(str(repo), "weather-app")
    now = time.time()
    chats = [("Add 7-day forecast endpoint", "codex", "gpt-6.2-sol"), ("Webhook retries with backoff", "claude",
             "claude-sonnet-5-5"), ("Fix flaky cache test", "claude", "claude-opus-5-5")]
    sids = []
    for i, (title, engine, model) in enumerate(chats):
        s = db.create_session(proj.id, engine, model, "medium", title=title)
        db.add_message(s.id, "user", "text", {"text": f"{title}. Keep it small and add a test."})
        db.add_message(s.id, "tool", "tool", {"name": "Edit" if engine == "claude" else "edit",
                                              "summary": "api/routes/forecast.py", "output": "", "is_error": False})
        db.add_message(s.id, "assistant", "text", {"text": "Done. Added the forecast route with a days parameter "
                                                   "(1-14), cached for 10 minutes, and documented it in the README."})
        db.add_message(s.id, "system", "usage", {"input": 18400 + 9000 * i, "output": 2300 + 400 * i,
                                                 "cached": 41000, "cost": 0.0 if engine == "codex" else 0.07})
        db.update_session(s.id, last_active=now - 3600 * (i + 1), status="done",
                          summary="Forecast endpoint with caching" if i == 0 else None)
        sids.append(s.id)
    db.add_message(sids[1], "assistant", "text", {"text": "The webhook sender now retries with exponential backoff "
                                                  "(1s, 2s, 4s, max 5 tries) and logs the final failure."})

    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 860)
    win.show()

    def shoot():
        win.open_session(sids[0])
        if which == "diff":
            from orchestrator.ui.diff_view import DiffDialog

            dlg = DiffDialog(str(repo), win)
            dlg.list.setCurrentRow(1)
            dlg.show()
        elif which == "search":
            win.searchPalette.popup()
            win.searchPalette.edit.setText("backoff")
            win.searchPalette.run()
        QTimer.singleShot(700, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
