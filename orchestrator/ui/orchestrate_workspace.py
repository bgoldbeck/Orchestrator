"""Orchestrate as one workspace, like a Claude project.

While a run is open, the planner chat stays on the left and you keep talking to it. The right pane lists the run's
tasks grouped as Waiting on you, Working, Queued and Done, one line of status each; opening a task shows its chat in
that pane (× goes back to the list). "Details" shows the full run page (models, queue order, Stop all, history),
and "New goal" goes back to the composer. Nothing here switches to the Chats page.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSplitter, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon as FIF,
    PushButton,
    SegmentedWidget,
    SmoothScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
)

from orchestrator.core import inbox as I
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database, Task
from orchestrator.ui.theme import ENGINE_COLORS, STATUS_COLORS, palette
from orchestrator.ui_text import relative

GROUPS = [("waiting", "Waiting on you"), ("working", "Working"), ("queued", "Queued"), ("done", "Done")]
ICONS = {"running": ("◐", STATUS_COLORS["running"]), "waiting": ("!", STATUS_COLORS["waiting"]),
         "proposed": ("?", STATUS_COLORS["waiting"]), "queued": ("○", STATUS_COLORS["idle"]),
         "done": ("✓", STATUS_COLORS["done"]), "failed": ("✕", STATUS_COLORS["error"]),
         "stopped": ("–", STATUS_COLORS["idle"])}


def group_of(t: Task) -> str:
    if t.status in ("waiting", "proposed"):
        return "waiting"
    if t.status == "running":
        return "working"
    if t.status == "queued":
        return "queued"
    return "done"


def _elapsed(t: Task, now: float) -> str:
    if not t.started_at:
        return ""
    m = int(((t.finished_at or now) - t.started_at) // 60)
    return "<1 min" if m < 1 else f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


class TaskRow(QFrame):
    clicked = Signal(int)  # task id

    def __init__(self, t: Task, line: str, when: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.tid = t.id
        p = palette()
        self.setObjectName("taskRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"#taskRow {{ background:transparent; border-radius:8px; }}"
                           f" #taskRow:hover {{ background:{p['card_hover']}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)
        glyph, col = ICONS.get(t.status, ("•", p["muted"]))
        icon = QLabel(glyph, self)
        icon.setFixedWidth(16)
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setStyleSheet(f"color:{col}; font-weight:700; background:transparent;")
        lay.addWidget(icon, 0, Qt.AlignTop)
        col_l = QVBoxLayout()
        col_l.setSpacing(1)
        title = StrongBodyLabel(t.title, self)
        title.setWordWrap(True)
        col_l.addWidget(title)
        sub = CaptionLabel(line, self)
        sub.setStyleSheet(f"color:{p['muted']};")
        sub.setWordWrap(True)
        self.sub = sub
        col_l.addWidget(sub)
        lay.addLayout(col_l, 1)
        right = QVBoxLayout()
        right.setSpacing(2)
        eng = CaptionLabel({"claude": "Claude", "codex": "Codex"}.get(t.engine, t.engine), self)
        eng.setStyleSheet(f"color:{ENGINE_COLORS.get(t.engine, p['muted'])};")
        right.addWidget(eng, 0, Qt.AlignRight)
        if when:
            w = CaptionLabel(when, self)
            w.setStyleSheet(f"color:{p['faint']};")
            right.addWidget(w, 0, Qt.AlignRight)
        right.addStretch(1)
        lay.addLayout(right)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.tid)
        super().mouseReleaseEvent(e)


class TaskPanel(SmoothScrollArea):
    """One run's tasks, grouped, plus the items that need you (questions, OKs, reviews) on top."""
    openTask = Signal(int)  # task id
    showDetails = Signal()

    def __init__(self, db: Database, om, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db, self.om = db, om
        self.oid: int | None = None
        self.collapsed: set[str] = {"done"}
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(10, 8, 12, 12)
        self.lay.setSpacing(2)
        self.setWidget(self.body)
        self._tick = QTimer(self, interval=3000, timeout=self._refresh_if_visible)
        self._tick.start()

    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self.reload()

    def set_run(self, oid: int | None) -> None:
        self.oid = oid
        self.reload()

    def _clear(self) -> None:
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def line_for(self, t: Task) -> str:
        if t.status in ("done", "failed", "stopped"):
            return ((t.result or {}).get("summary") or t.status.capitalize())[:160]
        if t.status == "queued":
            pos = self.om.queue_positions().get(t.id) if self.om is not None else None
            return f"Waiting for a free agent · #{pos} in line" if pos else "Waiting for a free agent"
        if t.status == "proposed":
            return "Needs your OK to start"
        if t.status == "waiting":
            return "Wants to use a tool: approve it in its chat"
        if self.om is not None and t.session_id:
            return self.om.last_line(t.session_id) or "Starting…"
        return "Running"

    def reload(self) -> None:
        self._clear()
        p = palette()
        if not self.oid:
            return
        now = time.time()
        tasks = self.db.tasks(self.oid)
        needs = [it for it in I.build(self.db, self.om) if it.orchestration_id == self.oid and not it.task_id]
        groups: dict[str, list[Task]] = {g: [] for g, _ in GROUPS}
        for t in tasks:
            groups[group_of(t)].append(t)
        for g, title in GROUPS:
            items = groups[g]
            n = len(items) + (len(needs) if g == "waiting" else 0)
            if not n:
                continue
            arrow = "▸" if g in self.collapsed else "▾"
            head = PushButton(f"{title} · {n}  {arrow}", self.body)
            head.setObjectName("groupHead")
            head.setStyleSheet(f"PushButton#groupHead {{ border:none; background:transparent; text-align:left;"
                               f" color:{p['muted']}; padding:8px 4px 2px 4px; }}")
            head.clicked.connect(lambda _=False, grp=g: self._toggle(grp))
            self.lay.addWidget(head)
            if g in self.collapsed:
                continue
            if g == "waiting":
                for it in needs:  # questions and run-level asks: answered on the Details tab
                    row = QFrame(self.body)
                    rl = QHBoxLayout(row)
                    rl.setContentsMargins(8, 6, 8, 6)
                    q = QLabel("?", row)
                    q.setFixedWidth(16)
                    q.setStyleSheet(f"color:{STATUS_COLORS['waiting']}; font-weight:700;")
                    rl.addWidget(q, 0, Qt.AlignTop)
                    lb = StrongBodyLabel(it.title, row)
                    lb.setWordWrap(True)
                    rl.addWidget(lb, 1)
                    b = PushButton("Answer", row)
                    b.clicked.connect(self.showDetails.emit)
                    rl.addWidget(b, 0, Qt.AlignTop)
                    self.lay.addWidget(row)
            for t in items:
                when = (relative(t.finished_at, now) if t.finished_at else _elapsed(t, now))
                row = TaskRow(t, self.line_for(t), when, self.body)
                row.clicked.connect(self.openTask.emit)
                self.lay.addWidget(row)
        if not tasks and not needs:
            e = CaptionLabel("No tasks yet. The planner is reading the goal and will delegate shortly.", self.body)
            e.setWordWrap(True)
            e.setStyleSheet(f"color:{p['muted']}; padding:8px;")
            self.lay.addWidget(e)
        self.lay.addStretch(1)

    def _toggle(self, g: str) -> None:
        self.collapsed ^= {g}
        self.reload()


class OrchestrateWorkspace(QWidget):
    def __init__(self, db: Database, mgr: SessionManager, om, parent: QWidget | None = None) -> None:
        from orchestrator.ui.chat_view import ChatView
        from orchestrator.ui.orchestrate_page import OrchestratePage

        super().__init__(parent)
        self.db, self.mgr, self.om = db, mgr, om
        self.setObjectName("orchestrateWorkspace")
        p = palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Horizontal, self)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        lay.addWidget(self.split)

        self.planner = ChatView(db, mgr, self)  # the orchestrator chat, always in view while a run is open
        self.planner.setMinimumWidth(380)
        self.planner.sideBySideRequested.connect(self.open_agent)
        self.planner.hide()
        self.split.addWidget(self.planner)

        self.side = QWidget(self)
        self.side.setObjectName("orchSide")
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.tabsBar = QFrame(self.side)
        self.tabsBar.setObjectName("tabsBar")
        self.tabsBar.setStyleSheet(f"#tabsBar {{ border-bottom:1px solid {p['border']}; }}")
        tb = QHBoxLayout(self.tabsBar)
        tb.setContentsMargins(12, 8, 8, 8)
        self.tabs = SegmentedWidget(self.tabsBar)
        self.tabs.addItem("tasks", "Tasks", lambda: self.show_tab("tasks"))
        self.tabs.addItem("details", "Details", lambda: self.show_tab("details"))
        tb.addWidget(self.tabs)
        tb.addStretch(1)
        self.newGoal = TransparentToolButton(FIF.ADD, self.tabsBar)
        self.newGoal.setToolTip("New goal (this run keeps going)")
        self.newGoal.clicked.connect(self.new_goal)
        tb.addWidget(self.newGoal)
        sl.addWidget(self.tabsBar)
        self.stack = QStackedWidget(self.side)
        self.tasks = TaskPanel(db, om, self.stack)
        self.tasks.openTask.connect(self.open_task)
        self.tasks.showDetails.connect(lambda: self.show_tab("details"))
        self.stack.addWidget(self.tasks)
        self.page = OrchestratePage(db, mgr, om, self.stack)
        self.stack.addWidget(self.page)
        self.agent = ChatView(db, mgr, self.stack)  # one sub-agent's chat, opened from the task list
        self.agent.set_side(True)
        self.agent.closeRequested.connect(self.show_board)
        self.agent.sideBySideRequested.connect(self.open_agent)
        self.stack.addWidget(self.agent)
        sl.addWidget(self.stack, 1)
        self.split.addWidget(self.side)

        self.page.openChat.connect(self.open_agent)
        self.page.runChanged.connect(self._run_changed)
        om.changed.connect(lambda oid: oid == self.page.current and self.tasks.isVisible() and self.tasks.reload())
        self._run_changed(self.page.current)

    def __getattr__(self, name: str):  # the main window and tests talk to the page through us
        if name in ("page", "planner", "agent", "side", "split", "stack", "tasks", "tabs") or name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.page, name)

    # ------------------------------------------------------------------ panes
    def planner_sid(self) -> int | None:
        o = self.db.orchestration(self.page.current) if self.page.current else None
        return o.session_id if o and o.session_id else None

    def in_run(self) -> bool:
        return self.planner_sid() is not None

    def _run_changed(self, _oid) -> None:
        sid = self.planner_sid()
        self.tasks.set_run(self.page.current)
        if sid:
            first = self.planner.isHidden()
            self.planner.show()
            self.tabsBar.show()
            if first:
                w = max(2, self.split.width())
                self.split.setSizes([w * 3 // 5, w - w * 3 // 5])
                self.show_tab("tasks")
            if not self.planner.session or self.planner.session.id != sid:
                self.planner.show_session(sid)
                if self.agent.session:
                    self.show_board()  # the open agent belongs to another run
        else:
            self.planner.hide()
            self.planner.show_session(None)
            self.tabsBar.hide()
            self.stack.setCurrentWidget(self.page)

    def show_tab(self, key: str) -> None:
        self.tabs.setCurrentItem(key)
        if key == "tasks":
            self.tasks.reload()
            self.stack.setCurrentWidget(self.tasks)
        else:
            self.stack.setCurrentWidget(self.page)
        if self.agent.session:
            self.agent.show_session(None)

    def show_board(self) -> None:
        self.show_tab("tasks" if self.in_run() else "details")

    def new_goal(self) -> None:
        self.page.history.setCurrentIndex(0)  # composer; planner pane hides until a run is chosen again

    def open_task(self, tid: int) -> None:
        t = self.db.task(tid)
        if t and t.session_id:
            self.open_agent(t.session_id)
        else:
            self.show_tab("details")

    def open_agent(self, sid: int) -> None:
        """Show a task's chat in the right pane. The planner itself just gets focus on the left."""
        if not sid:
            return
        if sid == self.planner_sid():
            self.planner.prompt.focus()
            return
        self.stack.setCurrentWidget(self.agent)
        self.agent.show_session(sid)

    def set_app_control(self, ctl) -> None:
        self.planner.set_app_control(ctl)
        self.agent.set_app_control(ctl)
