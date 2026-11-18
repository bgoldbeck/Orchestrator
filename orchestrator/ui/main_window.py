"""Top-level Fluent window."""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QWidget
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
    def __init__(self, db: Database, mgr: SessionManager, icon: QIcon | None = None) -> None:
        super().__init__()
        self.db = db
        self.setWindowTitle("Orchestrator")
        if icon:
            self.setWindowIcon(icon)
        self.resize(1320, 860)
        self.setMinimumSize(QSize(980, 640))
        self.chats = ChatsPage(db, mgr, self)
        self.settings = SettingsPage(db, self)
        self.addSubInterface(self.chats, FIF.CHAT, "Chats")
        self.addSubInterface(self.settings, FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM)
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setCollapsible(True)
        self.chats.chat.openSettingsRequested.connect(lambda: self.switchTo(self.settings))
        self.stackedWidget.currentChanged.connect(self._on_page)

    def _on_page(self, _i: int) -> None:
        if self.stackedWidget.currentWidget() is self.settings:
            self.settings.refresh()
