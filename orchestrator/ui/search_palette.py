"""Ctrl+K: search across all chats (titles, summaries, message text) and jump to one."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ListWidget, SearchLineEdit, isDarkTheme

from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database
from orchestrator.ui.theme import palette
from orchestrator.ui_text import age

SHORTCUTS = [
    ("Ctrl+K", "Search all chats"),
    ("Ctrl+N", "New chat in the current project"),
    ("Ctrl+L", "Focus the message box"),
    ("Ctrl+.", "Stop the current chat"),
    ("Ctrl+D", "Show changes (git diff) for the current chat"),
    ("Ctrl+1 … Ctrl+4", "Projects, Chats, Orchestrate, Schedules"),
    ("Ctrl+,", "Settings"),
    ("Enter / Shift+Enter", "Send / new line"),
    ("F1", "This list"),
]


class SearchPalette(QFrame):
    """A floating search box over the main window."""

    openSession = Signal(int)

    def __init__(self, db: Database, parent: QWidget) -> None:
        super().__init__(parent)
        self.db = db
        p = palette()
        bg = "#202020" if isDarkTheme() else "#fbfbfb"
        self.setObjectName("searchPalette")
        self.setStyleSheet(f"#searchPalette {{ background:{bg}; border:1px solid {p['border']}; border-radius:12px; }}")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        self.setGraphicsEffect(shadow)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        self.edit = SearchLineEdit(self)
        self.edit.setPlaceholderText("Search all chats: titles, summaries and messages")
        self.edit.textChanged.connect(lambda _t: self._timer.start(140))
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self.list = ListWidget(self)
        self.list.itemActivated.connect(self._open)
        self.list.itemClicked.connect(self._open)
        lay.addWidget(self.list, 1)
        self.hint = CaptionLabel("↑↓ to move  ·  Enter to open  ·  Esc to close", self)
        self.hint.setStyleSheet(f"color:{p['faint']};")
        lay.addWidget(self.hint)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.run)
        self.hide()

    def popup(self) -> None:
        w = self.parentWidget()
        width = min(680, w.width() - 80)
        self.setGeometry((w.width() - width) // 2, 70, width, min(460, w.height() - 120))
        self.edit.clear()
        self.run()
        self.show()
        self.raise_()
        self.edit.setFocus()

    def run(self) -> None:
        q = self.edit.text().strip()
        self.list.clear()
        if len(q) < 2:
            rows = [{"session_id": s.id, "snippet": "", "where": "recent"} for s in self.db.all_sessions(12)]
        else:
            rows = self.db.search_messages(q, 40)
        for r in rows:
            s = self.db.session(r["session_id"])
            if not s:
                continue
            proj = self.db.project(s.project_id)
            line1 = f"{s.title}    —    {proj.name if proj else ''} · {ENGINES.get(s.engine, s.engine)} · {age(s.last_active)}"
            text = line1 + (f"\n{r['snippet']}" if r["snippet"] and r["snippet"] != s.title else "")
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, s.id)
            self.list.addItem(it)
        if not self.list.count():
            it = QListWidgetItem("No chats match." if q else "No chats yet.")
            it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it)
        self.list.setCurrentRow(0)

    def _open(self, item: QListWidgetItem) -> None:
        sid = item.data(Qt.UserRole)
        if sid:
            self.hide()
            self.openSession.emit(int(sid))

    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        if obj is self.edit and e.type() == QEvent.KeyPress:
            k = QKeyEvent(e).key()
            if k == Qt.Key_Escape:
                self.hide()
                return True
            if k in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self.list.currentItem():
                    self._open(self.list.currentItem())
                return True
        return super().eventFilter(obj, e)

    def focusOutEvent(self, e) -> None:  # noqa: N802
        super().focusOutEvent(e)
