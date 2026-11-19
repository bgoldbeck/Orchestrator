"""MCP servers page screenshot with demo configs only (nothing is added or removed).

    .venv\\Scripts\\python scripts\\shot_mcp.py  -> docs/screenshots/mcp-servers.png
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = ROOT / "docs" / "screenshots" / "mcp-servers.png"
    home = Path(tempfile.mkdtemp(prefix="orch-shot-"))
    os.environ["ORCHESTRATOR_HOME"] = str(home)
    os.environ["ORCHESTRATOR_INDEX_CLAUDE"] = str(home / "none")
    os.environ["ORCHESTRATOR_INDEX_CODEX"] = str(home / "none")
    os.environ["ORCHESTRATOR_NO_USER_ENV"] = "1"
    cj = home / "claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"spelldirector": {"type": "stdio", "command": "node", "args": ["D:/Tools/spelldirector/index.js"]},
                       "midimcp": {"type": "http", "url": "http://192.168.1.140:8765/mcp"},
                       "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                                  "env": {"GITHUB_TOKEN": "x"}}},
        "projects": {"D:/Demo/weather-app": {"mcpServers": {"weather-db": {"command": "uvx", "args": ["mcp-sqlite"]}}}},
    }))
    cc = home / "config.toml"
    cc.write_text('[mcp_servers.github]\ncommand = "npx"\nargs = ["-y", "@modelcontextprotocol/server-github"]\n'
                  '[mcp_servers.docs]\nurl = "https://docs.example.com/mcp"\n')
    os.environ["ORCHESTRATOR_CLAUDE_JSON"] = str(cj)
    os.environ["ORCHESTRATOR_CODEX_CONFIG"] = str(cc)

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
    db.set_setting("context.last.claude", {"mcp": [
        {"server": "spelldirector", "status": "connected", "tools": 10, "tokens": 4200},
        {"server": "midimcp", "status": "failed", "tools": 0, "tokens": 0},
        {"server": "github", "status": "connected", "tools": 26, "tokens": 9100},
        {"server": "claude.ai Canva", "status": "connected", "source": "plugin", "tools": 30, "tokens": 6000}]})
    db.set_setting("mcp.off", ["claude:midimcp"])
    win = MainWindow(db, SessionManager(db), _app_icon())
    win.setMicaEffectEnabled(False)
    win.resize(1320, 860)
    win.show()

    def shoot():
        win.switchTo(win.mcp)
        QTimer.singleShot(600, lambda: (win.grab().save(str(out)), print("screenshot:", out), app.quit()))

    QTimer.singleShot(700, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
