"""Application entry: Qt + qasync event loop, theme, main window."""
from __future__ import annotations

import asyncio
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
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
    """Runs ``then`` on the next event-loop turn after the window's first paint."""

    def __init__(self, parent, then) -> None:
        super().__init__(parent)
        self._then = then

    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        if e.type() == QEvent.Paint:
            obj.removeEventFilter(self)
            QTimer.singleShot(0, self._then)  # after this paint is on screen
        return False


def _after_first_paint(win, then) -> None:
    win._first_paint = _FirstPaint(win, then)
    win.installEventFilter(win._first_paint)


def _preload(modules: list[str]) -> None:
    """Import modules on a background thread so their first real use does not stall the UI."""
    import importlib
    import threading

    def run() -> None:
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception:
                pass  # the real import reports problems where it matters

    threading.Thread(target=run, name="orch-preload", daemon=True).start()


def _tray_available() -> bool:
    from PySide6.QtWidgets import QSystemTrayIcon

    return QSystemTrayIcon.isSystemTrayAvailable()


def main() -> int:
    if "--mcp-server" in sys.argv:  # the built exe doubles as the orchestrator MCP server (stdio)
        from orchestrator.mcp_server import main as mcp_main

        mcp_main()
        return 0
    from orchestrator import startup
    from orchestrator.core import auth
    from orchestrator.lazy_imports import defer_qfw_image_utils

    auth.scrub_inherited_host_env()
    defer_qfw_image_utils()  # skip numpy/scipy/PIL until something actually blurs an image

    import qasync

    from orchestrator.core.scheduler import ScheduleRunner
    from orchestrator.core.session_index import SessionIndexer
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database
    from orchestrator.ui.main_window import MainWindow

    startup.mark("imports")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Orchestrator")
    app.setOrganizationName("Orchestrator")
    db = Database()
    from orchestrator.ui import appearance

    appearance.apply(appearance.load(db), emit=False)  # fonts, sizes, density, theme, accent (Settings › Appearance)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    closing = asyncio.Event()
    app.aboutToQuit.connect(closing.set)

    startup.mark("qapp")
    mgr = SessionManager(db)
    # Shell first: Projects home from the local database; other pages, the session scan, schedules,
    # tray and shortcuts are set up right after the first paint (MainWindow.finish_startup).
    win = MainWindow(db, mgr, _app_icon(), indexer=SessionIndexer(db), runner=ScheduleRunner(db, mgr), lazy=True)
    startup.mark("window_created")
    if startup.bench_quit():
        startup.on_ready = lambda: QTimer.singleShot(300, win.quit_app)

    def first_paint() -> None:
        startup.part_done("first_paint")
        _preload(["apscheduler.jobstores.sqlalchemy", "apscheduler.schedulers.asyncio"])
        win.finish_startup()
        # The Claude SDK is only needed for the first Claude turn; warm it once everything else is up.
        QTimer.singleShot(1500, lambda: _preload(["claude_agent_sdk"]))

    if "--tray" in sys.argv and _tray_available():
        QTimer.singleShot(0, first_paint)  # started at login: stay in the tray (window never painted)
    else:
        _after_first_paint(win, first_paint)
        win.show()

    with loop:
        loop.run_until_complete(closing.wait())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
