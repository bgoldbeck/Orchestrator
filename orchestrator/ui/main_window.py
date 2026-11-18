"""Top-level Fluent window, plus tray / close-to-tray wiring."""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QSystemTrayIcon, QWidget
from qfluentwidgets import FluentIcon as FIF, FluentWindow, NavigationItemPosition

from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database
from orchestrator.ui.chat_view import ChatView
from orchestrator.ui.settings_page import SettingsPage
from orchestrator.ui.sidebar import Sidebar


class ChatsPage(QWidget):
    def __init__(self, db: Database, mgr: SessionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatsPage")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.sidebar = Sidebar(db, mgr, self)
        self.chat = ChatView(db, mgr, self)
        lay.addWidget(self.sidebar)
        lay.addWidget(self.chat, 1)
        self.sidebar.sessionSelected.connect(self.chat.show_session)
        self.chat.newChatRequested.connect(self.sidebar.new_chat)
        self.chat.addProjectRequested.connect(self.sidebar.add_project)


class MainWindow(FluentWindow):
    def __init__(self, db: Database, mgr: SessionManager, icon: QIcon | None = None, indexer=None,
                 runner=None) -> None:
        super().__init__()
        self.db = db
        self.mgr = mgr
        self._quitting = False
        self.setWindowTitle("Orchestrator")
        self.app_icon = icon or QIcon()
        if icon:
            self.setWindowIcon(icon)
        self.resize(1320, 860)
        self.setMinimumSize(QSize(980, 640))
        self.chats = ChatsPage(db, mgr, self)
        self.settings = SettingsPage(db, self)
        self.addSubInterface(self.chats, FIF.CHAT, "Chats")
        self.runner = runner
        self.schedules = None
        if runner is not None:
            from orchestrator.ui.schedules_page import SchedulesPage

            runner.start()
            self.schedules = SchedulesPage(db, runner, self)
            self.addSubInterface(self.schedules, FIF.DATE_TIME, "Schedules")
            self.schedules.openChat.connect(self.open_session)
            self.chats.chat.runOnceRequested.connect(self._run_once_from_chat)
        self.addSubInterface(self.settings, FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM)
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setCollapsible(True)
        self.chats.chat.openSettingsRequested.connect(lambda: self.switchTo(self.settings))
        self.stackedWidget.currentChanged.connect(self._on_page)
        self.indexer = indexer
        if indexer is not None:
            indexer.changed.connect(self.chats.sidebar.reload)
            indexer.start()

        self.tray = None
        if runner is not None and QSystemTrayIcon.isSystemTrayAvailable():
            from orchestrator.ui.tray import Tray

            self.tray = Tray(db, mgr, runner, self.app_icon, self)
            self.tray.openRequested.connect(self.bring_to_front)
            self.tray.openSession.connect(self.open_session)
            self.tray.quitRequested.connect(self.quit_app)
            QApplication.instance().setQuitOnLastWindowClosed(False)
            runner.run_started.connect(lambda *_: self.chats.sidebar.reload())

    def _on_page(self, _i: int) -> None:
        if self.stackedWidget.currentWidget() is self.settings:
            self.settings.refresh()
        elif self.schedules is not None and self.stackedWidget.currentWidget() is self.schedules:
            self.schedules.reload()

    def _run_once_from_chat(self, prefill: dict, anchor) -> None:
        if self.schedules is None:
            return
        self.schedules.run_once(prefill, anchor)

    # ---- tray behavior
    def bring_to_front(self) -> None:
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    def open_session(self, sid: int) -> None:
        self.bring_to_front()
        self.switchTo(self.chats)
        self.chats.sidebar.select(sid)

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.tray is not None and not self._quitting and self.db.get_setting("ui.close_to_tray", True):
            e.ignore()
            self.hide()
            if not self.db.get_setting("ui.tray_hint_shown", False):
                self.db.set_setting("ui.tray_hint_shown", True)
                self.tray.notify("Orchestrator is still running",
                                 "Schedules keep running in the tray. Right-click the tray icon to quit.")
            return
        self._shutdown()
        super().closeEvent(e)
        QApplication.instance().quit()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()

    def _shutdown(self) -> None:
        if self.runner is not None:
            self.runner.shutdown()
        if self.tray is not None:
            self.tray.hide()
