"""Application entry: Qt + qasync event loop, theme, main window."""
from __future__ import annotations

import asyncio
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


def _app_icon() -> QIcon:
    """The app icon at every size (rendered from assets/icon.svg by scripts/make_icon.py)."""
    from orchestrator.ui.app_icon import app_icon

    return app_icon()


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
    from orchestrator import single_instance as single

    guard = None
    if not single.disabled():
        key = single.instance_key()
        guard = single.Mutex(key)
        if not guard.acquire():  # already running for this user + data folder: hand over and leave
            from PySide6.QtCore import QCoreApplication

            _qc = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])  # noqa: F841 - for QLocalSocket
            single.forward(key, sys.argv[1:])
            return 0
    from orchestrator import startup
    from orchestrator.core import auth
    from orchestrator.lazy_imports import defer_qfw_image_utils

    auth.scrub_inherited_host_env()
    from orchestrator.ui.app_icon import set_app_user_model_id

    set_app_user_model_id()  # before any window: taskbar shows our icon, not pythonw's
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
    icon = _app_icon()
    app.setWindowIcon(icon)  # every window and dialog, plus the taskbar button
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
    win = MainWindow(db, mgr, icon, indexer=SessionIndexer(db), runner=ScheduleRunner(db, mgr), lazy=True)
    startup.mark("window_created")
    if guard is not None:
        def second_launch(args: list[str]) -> None:
            if "--tray" in args:
                return  # start-at-login while already running: nothing to show
            single.bring_window_to_front(win)

        win._instance_server = single.listen(key, second_launch, win)
        app.aboutToQuit.connect(guard.release)
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
