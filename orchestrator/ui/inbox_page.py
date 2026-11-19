"""Waiting on you: one list of everything that needs the user, from every run (see core.inbox).

Each card shows what it is, why it waits and how many tasks it holds up, with the recommended answer as the
filled button. Questions also take a typed answer.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
)

from orchestrator.core import inbox as I
from orchestrator.storage.db import Database
from orchestrator.ui.theme import palette, ui_pt
from orchestrator.ui_text import relative

REFRESH_MS = 20_000
KIND_COLORS = {"question": "#2b6cb0", "proposal": "#b7791f", "approval": "#b7791f", "review": "#2f855a",
               "conflict": "#c2413b", "held": "#c2413b", "silent": "#718096", "paused": "#6b46c1"}


class InboxCard(QFrame):
    def __init__(self, page: "InboxPage", item: I.Item) -> None:
        super().__init__()
        self.page, self.item = page, item
        p = palette()
        col = KIND_COLORS.get(item.kind, "#888")
        self.setObjectName("inboxCard")
        self.setStyleSheet(f"#inboxCard {{ background:{p['card']}; border:1px solid {p['border']};"
                           f" border-left:4px solid {col}; border-radius:8px; }}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(8)
        kind = QLabel(I.KIND_TITLES.get(item.kind, item.kind), self)
        kind.setObjectName("kind")
        kind.setStyleSheet(f"QLabel#kind {{ color:#fff; background:{col}; border-radius:9px; padding:1px 8px;"
                           f" font-size:{ui_pt(8.5)}; font-weight:600; }}")
        top.addWidget(kind)
        if item.task_id:
            ident = CaptionLabel(f"T-{item.task_id}", self)
            ident.setStyleSheet(f"color:{p['muted']}; font-weight:600;")
            top.addWidget(ident)
        top.addStretch(1)
        if item.unblocks:
            ub = CaptionLabel(f"holds up {item.unblocks} task{'s' if item.unblocks != 1 else ''}", self)
            ub.setStyleSheet(f"color:{p['muted']};")
            top.addWidget(ub)
        if item.created_at:
            age = CaptionLabel(relative(item.created_at), self)
            age.setStyleSheet(f"color:{p['faint']};")
            top.addWidget(age)
        lay.addLayout(top)

        title = StrongBodyLabel(item.title, self)
        title.setWordWrap(True)
        lay.addWidget(title)
        if item.detail:
            d = BodyLabel(item.detail, self)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{p['muted']};")
            lay.addWidget(d)

        row = QHBoxLayout()
        row.setSpacing(6)
        for a in item.actions:
            b = (PrimaryPushButton if a.primary else PushButton)(a.label, self)
            b.clicked.connect(lambda _=False, act=a.id: self.page.act(self.item, act))
            row.addWidget(b)
        if item.free_text:
            self.answer = LineEdit(self)
            self.answer.setPlaceholderText("Or type an answer and press Enter")
            self.answer.returnPressed.connect(lambda: self.page.act(self.item, "answer", self.answer.text()))
            row.addWidget(self.answer, 1)
        else:
            row.addStretch(1)
        lay.addLayout(row)


class InboxPage(QWidget):
    openChat = Signal(int)
    countChanged = Signal(int)

    def __init__(self, db: Database, om=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db, self.om = db, om
        self.setObjectName("inboxPage")
        self.items: list[I.Item] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 16)
        root.setSpacing(12)
        bar = QHBoxLayout()
        bar.addWidget(TitleLabel("Waiting on you", self))
        bar.addStretch(1)
        root.addLayout(bar)
        self.sub = CaptionLabel("", self)
        self.sub.setStyleSheet(f"color:{palette()['muted']};")
        root.addWidget(self.sub)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(0, 0, 8, 0)
        self.list.setSpacing(10)
        self.list.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        if om is not None:
            om.changed.connect(lambda _oid: self._debounce.start())
            om.task_changed.connect(lambda _oid, _tid: self._debounce.start())
            if hasattr(om, "question_asked"):
                om.question_asked.connect(lambda _oid, _qid: self._debounce.start())
        self._debounce = QTimer(self, singleShot=True, interval=200, timeout=self.reload)
        self._tick = QTimer(self, interval=REFRESH_MS, timeout=self.reload)
        self._tick.start()
        self.reload()

    def reload(self) -> None:
        self.items = I.build(self.db, self.om)
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.items:
            empty = BodyLabel("Nothing needs you right now. Questions, merges and stuck tasks show up here.", self.body)
            empty.setStyleSheet(f"color:{palette()['faint']}; padding:8px 2px;")
            self.list.addWidget(empty)
        for it in self.items:
            self.list.addWidget(InboxCard(self, it))
        n = len(self.items)
        self.sub.setText("Most important first: what holds up the most work, then the oldest." if n else "")
        self.countChanged.emit(n)

    def act(self, item: I.Item, action: str, text: str = "") -> None:
        if action == "open" and item.session_id:
            self.openChat.emit(item.session_id)
            return
        if action == "diff" and item.task_id:
            from orchestrator.ui.diff_view import DiffDialog

            t = self.db.task(item.task_id)
            if t:
                DiffDialog(t.worktree or t.cwd or "", self.window(), f"Changes · {t.title}").exec()
            return
        if action == "answer" and not text.strip():
            return
        if self.om is None:
            return

        async def go() -> None:
            await I.perform(self.db, self.om, item, action, text)
            self.reload()

        asyncio.ensure_future(go())
