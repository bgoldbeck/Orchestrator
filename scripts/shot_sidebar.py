"""Chat list screenshot (demo data only): sections, filter chips, history limit.

    .venv\\Scripts\\python scripts\\shot_sidebar.py  -> docs/screenshots/chats-sidebar.png
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = ROOT / "docs" / "screenshots" / "chats-sidebar.png"
    home = Path(tempfile.mkdtemp(prefix="orch-shot-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.sidebar import Sidebar
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS

    app = QApplication(sys.argv)
    app.setFont(QFont(UI_FONT, 10))
    setFontFamilies(UI_FONTS)
    setTheme(Theme.DARK)
    setThemeColor(ACCENTS["Sky"])
    asyncio.set_event_loop(asyncio.new_event_loop())
    db = Database()
    now = time.time()
    p = db.add_project(r"C:\Unity\sparkfall", "sparkfall")
    o = db.add_orchestration(p.id, "Initial test. Reply when done", "claude", "claude-opus-5-5", "medium", 2)
    ps = db.create_session(p.id, "claude", "claude-opus-5-5", "medium", title="Orchestrate · Initial test")
    db.update_orchestration(o.id, session_id=ps.id)
    db.update_session(ps.id, last_active=now - 8 * 3600)
    for eng, title, ago in (("claude", "Set up player controller", 8), ("codex", "Rig the low-poly snake", 23)):
        s = db.create_session(p.id, eng, "", "", parent_id=ps.id, title=title)
        db.update_session(s.id, last_active=now - ago * 3600)
    for eng, title, ago in (("claude", "Fix the dash cooldown bug", 3), ("codex", "Particle trail for sparks", 26)):
        s = db.create_session(p.id, eng, "", "", title=title)
        db.update_session(s.id, last_active=now - ago * 3600)
    hist = ["Add Unity MCP and test scene", "File: inspector.png", "Hook up the sound bank", "read player.log",
            "Lighting presets dropdown", "Grass shader too bright"]
    for i, t in enumerate(hist):
        db.upsert_imported(SimpleNamespace(engine="claude", session_id=f"h{i}", title=t, last_active=now - (30 + i * 9) * 3600,
                                           path=f"/x/{i}.jsonl", mtime=1.0, cwd=p.path, model="", effort="",
                                           origin="app" if i % 2 else ""), p.id)
    db.add_project(r"D:\Orchestrator", "Orchestrator")
    db.add_project(r"D:\Demo\weather-app", "weather-app")
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    host.setStyleSheet("background:#202020;")
    QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
    sb = Sidebar(db, SessionManager(db), host)
    host.layout().addWidget(sb)
    sb.current_sid = ps.id
    sb.reload()
    host.resize(280, 600)
    host.show()
    app.processEvents()
    host.grab().save(str(out))
    print("screenshot:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
