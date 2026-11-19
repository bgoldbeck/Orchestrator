"""Usage screenshots with demo data only.

    .venv\\Scripts\\python scripts\\shot_usage.py page  -> docs/screenshots/usage-page.png (whole window, Usage page)
    .venv\\Scripts\\python scripts\\shot_usage.py bar   -> docs/screenshots/usage-bar.png (title bar pills + tooltip)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def demo_logs(home: Path) -> None:
    now = time.time()
    d = home / "codex" / "sessions" / "2026" / "11" / "18"
    d.mkdir(parents=True)
    rl = {"limit_id": "codex",
          "primary": {"used_percent": 46.0, "window_minutes": 300, "resets_at": now + 2 * 3600 + 900},
          "secondary": {"used_percent": 74.0, "window_minutes": 10080, "resets_at": now + 3 * 86400},
          "credits": {"has_credits": False}}
    line = {"timestamp": iso(now - 120), "type": "event_msg",
            "payload": {"type": "token_count", "info": None, "rate_limits": rl}}
    (d / "rollout-demo.jsonl").write_text(json.dumps(line), "utf-8")
    proj = home / "claude" / "demo"
    proj.mkdir(parents=True)
    rows = [{"type": "assistant", "timestamp": iso(now - i * 1500),
             "message": {"id": f"m{i}", "usage": {"input_tokens": 9000, "cache_creation_input_tokens": 20000,
                                                  "output_tokens": 4000}}} for i in range(40)]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows), "utf-8")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "page"
    out = Path(os.environ["SHOT_OUT"]) if os.environ.get("SHOT_OUT") else ROOT / "docs" / "screenshots" / f"usage-{which}.png"
    home = Path(tempfile.mkdtemp(prefix="orch-usage-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home / "home")
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "claude")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "codex")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"
    demo_logs(home)

    import asyncio

    import qasync
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.app import _app_icon
    from orchestrator.core import usage as U
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
    db.add_project(r"D:\Demo\weather-app", "weather-app")
    now = time.time()
    U.record_claude_rate_limit(db, {"rate_limit_type": "seven_day", "status": "allowed_warning",
                                    "utilization": 0.88, "resets_at": now + 2 * 86400 + 5 * 3600})
    # a little history so the sparklines show
    hist = []
    for i, (c5, cw, aw) in enumerate([(12, 60, 80), (20, 63, 82), (31, 66, 84), (38, 70, 86), (46, 74, 88)]):
        t = now - (5 - i) * 3600
        hist += [{"p": "codex", "w": "five_hour", "v": 100.0 - c5, "t": t, "r": None},
                 {"p": "codex", "w": "weekly", "v": 100.0 - cw, "t": t, "r": None},
                 {"p": "claude", "w": "weekly", "v": 100.0 - aw, "t": t, "r": None}]
    db.set_setting(U.HISTORY_KEY, hist)  # percent left
    mgr = SessionManager(db)
    win = MainWindow(db, mgr, _app_icon())
    win.setMicaEffectEnabled(False)
    if os.environ.get("SHOT_THEME"):  # e.g. SHOT_THEME=Black
        from orchestrator.ui import appearance as A

        A.apply(A.Look(theme=os.environ["SHOT_THEME"]))
    win.resize(1320, 860)
    win.show()

    def shoot():
        if which == "page":
            win.switchTo(win.usage)
        else:
            pill = win.usageBar.pills["codex"]
            from qfluentwidgets import ToolTip

            tip = ToolTip(pill.toolTip(), win)
            tip.move(pill.mapTo(win, QPoint(0, pill.height() + 6)) + QPoint(-120, 0))
            tip.show()

        def grab():
            img = win.grab()  # render the window itself (other apps on screen never show up)
            if which == "bar":
                from PySide6.QtGui import QPainter

                p = QPainter(img)
                p.drawPixmap(tip.mapTo(win, QPoint(0, 0)) if tip.parent() is win else tip.pos(), tip.grab())
                p.end()
                img = img.copy(img.width() - 760, 0, 760, 230)
            img.save(str(out))
            print("screenshot:", out)
            app.quit()

        QTimer.singleShot(900, grab)

    QTimer.singleShot(900, shoot)
    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
