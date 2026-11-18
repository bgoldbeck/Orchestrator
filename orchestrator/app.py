"""Application entry: Qt + qasync event loop, theme, main window."""
from __future__ import annotations

import asyncio
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#4cc2ff"))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setBrush(QColor("#0b1220"))
    for x, y in ((20, 20), (44, 20), (32, 44)):
        p.drawEllipse(x - 7, y - 7, 14, 14)
    p.end()
    return QIcon(pm)


class _FirstPaint(QObject):
    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        if e.type() == QEvent.Paint:
            from orchestrator import startup

            obj.removeEventFilter(self)
            QTimer.singleShot(0, lambda: startup.part_done("first_paint"))  # after this paint is on screen
        return False


def _watch_first_paint(win) -> None:
    win._first_paint = _FirstPaint(win)
    win.installEventFilter(win._first_paint)


def main() -> int:
    if "--mcp-server" in sys.argv:  # the built exe doubles as the orchestrator MCP server (stdio)
        from orchestrator.mcp_server import main as mcp_main

        mcp_main()
        return 0
    from orchestrator.core import auth

    auth.scrub_inherited_host_env()

    import qasync
    from qfluentwidgets import Theme, setFontFamilies, setTheme, setThemeColor

    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.core.session_index import SessionIndexer
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow
    from orchestrator.ui.theme import ACCENTS, UI_FONT, UI_FONTS
    from orchestrator import startup

    startup.mark("imports")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Orchestrator")
    app.setOrganizationName("Orchestrator")
    font = QFont(UI_FONT)
    font.setPointSizeF(10)
    app.setFont(font)
    setFontFamilies(UI_FONTS)

    db = Database()
    setTheme({"Dark": Theme.DARK, "Light": Theme.LIGHT, "Auto": Theme.AUTO}[db.get_setting("ui.theme", "Dark")])
    setThemeColor(ACCENTS.get(db.get_setting("ui.accent", "Sky"), ACCENTS["Sky"]))

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    closing = asyncio.Event()
    app.aboutToQuit.connect(closing.set)

    startup.mark("qapp")
    mgr = SessionManager(db)
    indexer = SessionIndexer(db)
    indexer.scanned.connect(lambda: startup.part_done("sessions"))
    win = MainWindow(db, mgr, _app_icon(), indexer=indexer, runner=ScheduleRunner(db, mgr))
    startup.mark("window_created")
    _watch_first_paint(win)
    if startup.bench_quit():
        startup.on_ready = lambda: QTimer.singleShot(300, win.quit_app)
    if "--tray" in sys.argv and win.tray is not None:
        pass  # started at login: stay in the tray
    else:
        win.show()

    with loop:
        loop.run_until_complete(closing.wait())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
