"""Left pane: projects (folders) with chats underneath."""
from __future__ import annotations

import math
import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QTreeWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    CaptionLabel,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    RoundMenu,
    SearchLineEdit,
    ToolTipFilter,
    TransparentToolButton,
    TreeItemDelegate,
    TreeWidget,
)

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database
from orchestrator.ui.theme import ENGINE_COLORS, STATUS_COLORS, palette
from orchestrator.ui_text import age

ROLE_KIND = Qt.UserRole
ROLE_ID = Qt.UserRole + 1
ROLE_ENGINE = Qt.UserRole + 2
ROLE_STATUS = Qt.UserRole + 3
ROLE_AGE = Qt.UserRole + 4
ROLE_TITLE = Qt.UserRole + 5


class ChatRowDelegate(TreeItemDelegate):
    """Session rows: status dot (pulses while running), engine badge, title, age."""

    def __init__(self, tree) -> None:
        super().__init__(tree)
        self.phase = 0.0

    def sizeHint(self, option, index):  # noqa: N802
        sz = super().sizeHint(option, index)
        return QSize(sz.width(), 30 if index.data(ROLE_KIND) == "header" else 32)

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        if index.data(ROLE_KIND) in ("session", "header"):
            option.text = ""
            option.icon = QIcon()

    def paint(self, painter: QPainter, option, index) -> None:
        kind = index.data(ROLE_KIND)
        if kind == "header":
            painter.save()
            painter.setPen(QColor(palette()["muted"]))
            f = QFont(option.font)
            f.setPointSizeF(7.5)
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1)
            painter.setFont(f)
            painter.drawText(option.rect.adjusted(4, 8, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, index.data(ROLE_TITLE))
            painter.restore()
            return
        super().paint(painter, option, index)
        if kind != "session":
            return
        r = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        status = index.data(ROLE_STATUS) or "idle"
        col = QColor(STATUS_COLORS.get(status, STATUS_COLORS["idle"]))
        cy = r.center().y()
        x = r.x() + 6
        if status in ("running", "waiting"):
            halo = QColor(col)
            halo.setAlphaF(0.18 + 0.22 * (0.5 + 0.5 * math.sin(self.phase)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(QPointF(x + 4, cy), 7, 7)
        painter.setPen(Qt.NoPen)
        painter.setBrush(col)
        painter.drawEllipse(QPointF(x + 4, cy), 3.5, 3.5)
        x += 16
        engine = index.data(ROLE_ENGINE) or ""
        painter.setBrush(QColor(ENGINE_COLORS.get(engine, "#888")))
        painter.drawRoundedRect(QRectF(x, cy - 7, 14, 14), 4, 4)
        painter.setPen(QColor("white"))
        bf = QFont(option.font)
        bf.setPixelSize(9)
        bf.setBold(True)
        painter.setFont(bf)
        painter.drawText(QRectF(x, cy - 7, 14, 14), Qt.AlignCenter, engine[:1].upper())
        x += 22
        age_txt = index.data(ROLE_AGE) or ""
        af = QFont(option.font)
        af.setPointSizeF(8)
        painter.setFont(af)
        aw = painter.fontMetrics().horizontalAdvance(age_txt)
        painter.setPen(QColor(palette()["faint"]))
        painter.drawText(QRectF(r.right() - aw - 14, r.y(), aw + 4, r.height()), Qt.AlignVCenter | Qt.AlignRight, age_txt)
        painter.setFont(QFont(option.font))
        title_w = r.right() - aw - 22 - x
        title = painter.fontMetrics().elidedText(index.data(ROLE_TITLE) or "", Qt.ElideRight, int(title_w))
        painter.setPen(QColor(palette()["text"]))
        painter.drawText(QRectF(x, r.y(), title_w, r.height()), Qt.AlignVCenter | Qt.AlignLeft, title)
        painter.restore()


class Sidebar(QFrame):
    sessionSelected = Signal(object)  # session id or None

    def __init__(self, db: Database, manager: SessionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mgr = manager
        self.current_sid: int | None = None
        p = palette()
        self.setObjectName("sidebar")
        self.setStyleSheet(f"#sidebar {{ background:{p['sidebar']}; border-right:1px solid {p['border']}; }}")
        self.setFixedWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(10)

        self.newBtn = PrimaryPushButton(FIF.ADD, "New chat", self)
        self.newBtn.setFixedHeight(36)
        self.newBtn.clicked.connect(self.new_chat)
        lay.addWidget(self.newBtn)

        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Search chats")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        head = QHBoxLayout()
        lbl = CaptionLabel("PROJECTS", self)
        lbl.setStyleSheet(f"color:{p['muted']}; font-weight:600; letter-spacing:1px;")
        add = TransparentToolButton(FIF.FOLDER_ADD, self)
        add.setFixedSize(28, 28)
        add.setIconSize(QSize(14, 14))
        add.setToolTip("Add project folder")
        add.installEventFilter(ToolTipFilter(add))
        add.clicked.connect(self.add_project)
        head.addWidget(lbl)
        head.addStretch(1)
        head.addWidget(add)
        lay.addLayout(head)

        self.tree = TreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setStyleSheet("TreeWidget, QTreeView { background:transparent; border:none; }")
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemClicked.connect(self._clicked)
        self.delegate = ChatRowDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)
        lay.addWidget(self.tree, 1)
        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._tick)
        self._pulse.start(90)
        self._ages = QTimer(self)
        self._ages.timeout.connect(self._refresh_ages)
        self._ages.start(60_000)

        manager.status_changed.connect(lambda sid, _s: self._refresh_item(sid))
        manager.session_updated.connect(self._refresh_item)
        self.reload()

    # ---- data
    def reload(self) -> None:
        expanded = {self.tree.topLevelItem(i).data(0, ROLE_ID) for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).isExpanded()}
        first = self.tree.topLevelItemCount() == 0
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        header_added = False
        for proj in self.db.projects():
            if proj.auto and not header_added:
                hi = QTreeWidgetItem([""])
                hi.setData(0, ROLE_KIND, "header")
                hi.setData(0, ROLE_TITLE, "DISCOVERED IN ENGINE HISTORY")
                hi.setFlags(Qt.NoItemFlags)
                self.tree.addTopLevelItem(hi)
                header_added = True
            pi = QTreeWidgetItem([proj.name])
            pi.setIcon(0, (FIF.HISTORY if proj.auto else FIF.FOLDER).icon())
            pi.setData(0, ROLE_KIND, "project")
            pi.setData(0, ROLE_ID, proj.id)
            pi.setToolTip(0, proj.path)
            self.tree.addTopLevelItem(pi)
            for sess in self.db.sessions(proj.id):
                ci = QTreeWidgetItem([sess.title])
                ci.setData(0, ROLE_KIND, "session")
                ci.setData(0, ROLE_ID, sess.id)
                self._decorate(ci, sess)
                pi.addChild(ci)
                if sess.id == self.current_sid:
                    self.tree.setCurrentItem(ci)
            pi.setExpanded((first and not proj.auto) or proj.id in expanded or any(
                pi.child(i).data(0, ROLE_ID) == self.current_sid for i in range(pi.childCount())))
        self._filter(self.search.text())
        self.tree.verticalScrollBar().setValue(scroll)

    def _decorate(self, item: QTreeWidgetItem, s) -> None:
        if isinstance(s, int):
            s = self.db.session(s)
        if not s:
            return
        item.setText(0, s.title)
        item.setData(0, ROLE_TITLE, s.title)
        item.setData(0, ROLE_ENGINE, s.engine)
        item.setData(0, ROLE_STATUS, s.status)
        item.setData(0, ROLE_AGE, age(s.last_active))
        src = "Imported from engine history" if s.source == "imported" else "Started in Orchestrator"
        item.setToolTip(0, f"{s.title}\n{ENGINES.get(s.engine, s.engine)} · {s.model or 'default model'} · "
                           f"{s.effort or 'default effort'}\n{src} · status: {s.status}")

    def _tick(self) -> None:
        self.delegate.phase += 0.35
        if self.mgr.any_running():
            self.tree.viewport().update()

    def _refresh_ages(self) -> None:
        for i in range(self.tree.topLevelItemCount()):
            pi = self.tree.topLevelItem(i)
            for j in range(pi.childCount()):
                self._decorate(pi.child(j), pi.child(j).data(0, ROLE_ID))

    def _find(self, sid: int) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            pi = self.tree.topLevelItem(i)
            for j in range(pi.childCount()):
                if pi.child(j).data(0, ROLE_ID) == sid:
                    return pi.child(j)
        return None

    def _refresh_item(self, sid: int) -> None:
        item = self._find(sid)
        if item:
            self._decorate(item, sid)
        else:
            self.reload()

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            pi = self.tree.topLevelItem(i)
            any_vis = False
            if pi.data(0, ROLE_KIND) == "header":
                pi.setHidden(bool(t))
                continue
            for j in range(pi.childCount()):
                ch = pi.child(j)
                vis = not t or t in ch.text(0).lower()
                ch.setHidden(not vis)
                any_vis |= vis
            pi.setHidden(bool(t) and not any_vis and t not in pi.text(0).lower())
            if t and any_vis:
                pi.setExpanded(True)

    # ---- actions
    def selected_project_id(self) -> int | None:
        item = self.tree.currentItem()
        if item:
            if item.data(0, ROLE_KIND) == "session":
                item = item.parent()
            return item.data(0, ROLE_ID)
        projects = self.db.projects()
        return projects[0].id if projects else None

    def add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self.window(), "Choose a project folder", os.path.expanduser("~"))
        if not path:
            return
        proj = self.db.add_project(os.path.normpath(path))
        self.reload()
        for i in range(self.tree.topLevelItemCount()):
            if self.tree.topLevelItem(i).data(0, ROLE_ID) == proj.id:
                self.tree.setCurrentItem(self.tree.topLevelItem(i))
        self.new_chat()

    def new_chat(self, project_id: int | None = None) -> None:
        pid = project_id or self.selected_project_id()
        if pid is None:
            self.add_project()
            return
        last = self.db.get_setting("last") or ["codex", *self.db.default_model("codex")]
        choice = model_registry.pick(self.db, *last)
        if choice is None:
            InfoBar.warning("No models enabled", "Turn on at least one model in Settings › Models.",
                            parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=4000)
            return
        s = self.db.create_session(pid, *choice)
        self.select(s.id)

    def select(self, sid: int | None) -> None:
        prev = self.current_sid
        self.current_sid = sid
        # Drop an untouched empty chat when leaving it.
        if prev and prev != sid and not self.db.messages(prev) and not self.mgr.is_running(prev):
            self.db.delete_session(prev)
        self.reload()
        item = self._find(sid) if sid else None
        if item:
            self.tree.setCurrentItem(item)
        self.sessionSelected.emit(sid)

    def _clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.data(0, ROLE_KIND) == "session":
            sid = item.data(0, ROLE_ID)
            if sid != self.current_sid:
                self.select(sid)

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        menu = RoundMenu(parent=self)
        kind, iid = item.data(0, ROLE_KIND), item.data(0, ROLE_ID)
        if kind == "project":
            menu.addAction(Action(FIF.ADD, "New chat here", triggered=lambda: self.new_chat(iid)))
            proj = self.db.project(iid)
            menu.addAction(Action(FIF.FOLDER, "Open folder", triggered=lambda: os.startfile(proj.path) if proj else None))
            menu.addSeparator()
            menu.addAction(Action(FIF.REMOVE, "Remove from list", triggered=lambda: self._remove_project(iid)))
        else:
            menu.addAction(Action(FIF.DELETE, "Delete chat", triggered=lambda: self._delete_chat(iid)))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _remove_project(self, pid: int) -> None:
        box = MessageBox("Remove project?", "This removes the project and its chats from Orchestrator. "
                         "Files on disk and the engines' own session logs are not touched.", self.window())
        if box.exec():
            self.db.remove_project(pid)
            if self.current_sid and not self.db.session(self.current_sid):
                self.current_sid = None
                self.sessionSelected.emit(None)
            self.reload()

    def _delete_chat(self, sid: int) -> None:
        if self.mgr.is_running(sid):
            return
        box = MessageBox("Delete chat?", "The chat is removed from Orchestrator. The engine's own session log is kept.",
                         self.window())
        if box.exec():
            self.db.delete_session(sid)
            if sid == self.current_sid:
                self.current_sid = None
                self.sessionSelected.emit(None)
            self.reload()
