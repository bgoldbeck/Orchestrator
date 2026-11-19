"""Orchestrate as one workspace, like a Claude project.

The header always shows the project (click to switch or add one) and, inside a run, the run's name (click to switch
runs in that project). With no run open the page is the project's home: a goal box and the project's runs. With a
run open the planner chat stays on the left and the right pane lists the run's tasks: what needs you on top (answer
right there), then Working, Queued and Done. Opening a task shows its chat in that pane (× goes back to the list).
Nothing here switches to the Chats page.
"""
from __future__ import annotations

import asyncio
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSplitter, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    CaptionLabel,
    FluentIcon as FIF,
    IconWidget,
    PushButton,
    RoundMenu,
    SmoothScrollArea,
    StrongBodyLabel,
    TransparentPushButton,
)

from orchestrator.core import inbox as I
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database, Task
from orchestrator.ui.theme import ENGINE_COLORS, STATUS_COLORS, palette, ui_pt
from orchestrator.ui_text import relative

GROUPS = [("waiting", "Needs you"), ("working", "Working"), ("queued", "Queued"), ("done", "Done")]
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

    menuRequested = Signal(int, object)  # task id, global pos

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.tid)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        self.menuRequested.emit(self.tid, e.globalPos())


class TaskPanel(SmoothScrollArea):
    """One run's tasks, grouped, plus the items that need you (questions, OKs, reviews) on top."""
    openTask = Signal(int)  # task id
    openChat = Signal(int)  # session id

    def __init__(self, db: Database, om, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db, self.om = db, om
        self.oid: int | None = None
        self.items: list[I.Item] = []
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
        from orchestrator.ui.inbox_page import InboxCard

        self._clear()
        p = palette()
        if not self.oid:
            return
        now = time.time()
        tasks = self.db.tasks(self.oid)
        self.items = [it for it in I.build(self.db, self.om) if it.orchestration_id == self.oid]
        covered = {it.task_id for it in self.items if it.task_id}
        groups: dict[str, list[Task]] = {g: [] for g, _ in GROUPS}
        for t in tasks:
            g = group_of(t)
            if g == "waiting" and t.id in covered:
                continue  # shown as a card with its buttons
            groups[g].append(t)
        for g, title in GROUPS:
            items = groups[g]
            n = len(items) + (len(self.items) if g == "waiting" else 0)
            if not n:
                continue
            arrow = "▸" if g in self.collapsed else "▾"
            head = PushButton(f"{title} · {n}  {arrow}", self.body)
            head.setObjectName("groupHead")
            col = STATUS_COLORS["waiting"] if g == "waiting" else p["muted"]
            head.setStyleSheet(f"PushButton#groupHead {{ border:none; background:transparent; text-align:left;"
                               f" color:{col}; padding:8px 4px 2px 4px; }}")
            head.clicked.connect(lambda _=False, grp=g: self._toggle(grp))
            self.lay.addWidget(head)
            if g in self.collapsed:
                continue
            if g == "waiting":
                for it in self.items:  # questions, OKs, reviews: answered right here
                    self.lay.addWidget(InboxCard(self, it))
                    self.lay.addSpacing(4)
            for t in items:
                when = (relative(t.finished_at, now) if t.finished_at else _elapsed(t, now))
                row = TaskRow(t, self.line_for(t), when, self.body)
                row.clicked.connect(self.openTask.emit)
                row.menuRequested.connect(self._menu)
                self.lay.addWidget(row)
        if not tasks and not self.items:
            e = CaptionLabel("No tasks yet. The planner is reading the goal and will delegate shortly.", self.body)
            e.setWordWrap(True)
            e.setStyleSheet(f"color:{p['muted']}; padding:8px;")
            self.lay.addWidget(e)
        self.lay.addStretch(1)

    def act(self, item: I.Item, action: str, text: str = "") -> None:
        """Buttons on the Needs you cards (same actions as the Waiting on you page)."""
        if action == "open" and item.session_id:
            self.openChat.emit(item.session_id)
            return
        if action == "diff" and item.task_id:
            self._diff(item.task_id)
            return
        if (action == "answer" and not text.strip()) or self.om is None:
            return

        async def go() -> None:
            await I.perform(self.db, self.om, item, action, text)
            self.reload()

        asyncio.ensure_future(go())

    def _diff(self, tid: int) -> None:
        from orchestrator.ui.diff_view import DiffDialog

        t = self.db.task(tid)
        if t:
            DiffDialog(t.worktree or t.cwd or "", self.window(), f"Changes · {t.title}").exec()

    def task_menu(self, tid: int) -> RoundMenu:
        from orchestrator.core.orchestration import ACTIVE

        t = self.db.task(tid)
        menu = RoundMenu(parent=self)
        if not t:
            return menu
        if t.session_id:
            menu.addAction(Action(FIF.CHAT, "Open chat", triggered=lambda: self.openTask.emit(tid)))
        if t.status not in ("proposed", "queued"):
            menu.addAction(Action(FIF.DOCUMENT, "Show changes", triggered=lambda: self._diff(tid)))
        if self.om is None:
            return menu
        pos = self.om.queue_positions()
        if t.status == "queued" and tid in pos:
            if pos[tid] > 1:
                menu.addAction(Action(FIF.UP, "Move earlier", triggered=lambda: self.om.move_task(tid, -1)))
            if pos[tid] < len(pos):
                menu.addAction(Action(FIF.DOWN, "Move later", triggered=lambda: self.om.move_task(tid, +1)))
        menu.addSeparator()
        if t.status in ACTIVE:
            menu.addAction(Action(FIF.CLOSE, "Cancel" if t.status == "queued" else "Stop",
                                  triggered=lambda: asyncio.ensure_future(self.om.stop_task(tid))))
        else:
            menu.addAction(Action(FIF.SYNC, "Retry", triggered=lambda: self.om.retry_task(tid)))
        return menu

    def _menu(self, tid: int, pos) -> None:
        self.task_menu(tid).exec(pos)

    def _toggle(self, g: str) -> None:
        self.collapsed ^= {g}
        self.reload()


class ProjectButton(QFrame):
    """Header project picker: folder icon, name, path, ▾. Click opens the project menu."""
    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        p = palette()
        self.setObjectName("projectBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Switch project")
        self.setStyleSheet(f"#projectBtn {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:8px; }}"
                           f" #projectBtn:hover {{ background:{p['card_hover']}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        ic = IconWidget(FIF.FOLDER, self)
        ic.setFixedSize(16, 16)
        lay.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name = StrongBodyLabel("", self)
        self.path = CaptionLabel("", self)
        self.path.setStyleSheet(f"color:{p['muted']};")
        col.addWidget(self.name)
        col.addWidget(self.path)
        lay.addLayout(col)
        arrow = QLabel("▾", self)
        arrow.setStyleSheet(f"color:{p['muted']}; background:transparent;")
        lay.addWidget(arrow)

    def set_project(self, name: str, path: str) -> None:
        self.name.setText(name)
        self.path.setText(path)
        self.path.setVisible(bool(path))

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


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
        lay.setSpacing(0)

        # header: project › run, always visible
        self.header = QFrame(self)
        self.header.setObjectName("orchHeader")
        self.header.setStyleSheet(f"#orchHeader {{ border-bottom:1px solid {p['border']}; }}")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(20, 10, 16, 10)
        hl.setSpacing(8)
        self.backBtn = PushButton(FIF.LEFT_ARROW, "All runs", self.header)
        self.backBtn.setObjectName("runsBack")
        self.backBtn.setToolTip("Back to this project's home: all runs and a new goal (Alt+Left). The run keeps going.")
        self.backBtn.clicked.connect(self.new_goal)
        hl.addWidget(self.backBtn)
        back = QShortcut(QKeySequence("Alt+Left"), self)
        back.setContext(Qt.WidgetWithChildrenShortcut)
        back.activated.connect(lambda: self.in_run() and self.new_goal())
        self.projectBtn = ProjectButton(self.header)
        self.projectBtn.clicked.connect(self._project_menu)
        hl.addWidget(self.projectBtn)
        self.crumb = QLabel("›", self.header)
        self.crumb.setStyleSheet(f"color:{p['faint']}; font-size:{ui_pt(16)}; background:transparent;")
        hl.addWidget(self.crumb)
        self.runBtn = TransparentPushButton("", self.header)
        self.runBtn.setToolTip("Switch to another run in this project")
        self.runBtn.clicked.connect(self._run_menu)
        hl.addWidget(self.runBtn)
        self.runState = QWidget(self.header)
        rs = QHBoxLayout(self.runState)
        rs.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.runState)
        hl.addStretch(1)
        self.settingsBtn = PushButton(FIF.SETTING, "Run settings", self.header)
        self.settingsBtn.clicked.connect(lambda: self.page.settings_flyout(self.settingsBtn))
        hl.addWidget(self.settingsBtn)
        self.stopBtn = PushButton(FIF.CLOSE, "Stop all", self.header)
        self.stopBtn.setToolTip("Stop the planner and every task in this run")
        self.stopBtn.clicked.connect(lambda: self.page._stop_all())
        hl.addWidget(self.stopBtn)
        self.newGoal = PushButton(FIF.ADD, "New goal", self.header)
        self.newGoal.setToolTip("Back to the project's home to start another goal (this run keeps going)")
        self.newGoal.clicked.connect(self.new_goal)
        hl.addWidget(self.newGoal)
        lay.addWidget(self.header)

        # body: the project's home, or the run (planner | tasks)
        self.body = QStackedWidget(self)
        lay.addWidget(self.body, 1)
        self.page = OrchestratePage(db, mgr, om, self.body)
        self.body.addWidget(self.page)
        self.split = QSplitter(Qt.Horizontal, self.body)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        self.body.addWidget(self.split)

        self.planner = ChatView(db, mgr, self.split)  # the orchestrator chat, always in view while a run is open
        self.planner.setMinimumWidth(380)
        self.planner.fixed_title = "Planner"
        self.planner.path.hide()  # the header above shows the project and run
        self.planner.sideBySideRequested.connect(self.open_agent)
        self.split.addWidget(self.planner)

        self.side = QWidget(self.split)
        self.side.setObjectName("orchSide")
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.sideHead = QFrame(self.side)
        self.sideHead.setObjectName("sideHead")
        self.sideHead.setStyleSheet(f"#sideHead {{ border-bottom:1px solid {p['border']}; }}")
        sh = QHBoxLayout(self.sideHead)
        sh.setContentsMargins(16, 10, 12, 10)
        self.sideTitle = StrongBodyLabel("Tasks", self.sideHead)
        sh.addWidget(self.sideTitle)
        sh.addStretch(1)
        self.sideInfo = CaptionLabel("", self.sideHead)
        self.sideInfo.setStyleSheet(f"color:{p['muted']};")
        sh.addWidget(self.sideInfo)
        sl.addWidget(self.sideHead)
        self.stack = QStackedWidget(self.side)
        self.tasks = TaskPanel(db, om, self.stack)
        self.tasks.openTask.connect(self.open_task)
        self.tasks.openChat.connect(self.open_agent)
        self.stack.addWidget(self.tasks)
        self.agent = ChatView(db, mgr, self.stack)  # one sub-agent's chat, opened from the task list
        self.agent.set_side(True)
        self.agent.closeRequested.connect(self.show_board)
        self.agent.sideBySideRequested.connect(self.open_agent)
        self.stack.addWidget(self.agent)
        sl.addWidget(self.stack, 1)
        self.split.addWidget(self.side)
        self._sized = False

        self.page.openChat.connect(self.open_agent)
        self.page.runChanged.connect(self._run_changed)
        self.page.shown.connect(self.refresh_header)
        self.page.projectsChanged.connect(self.refresh_header)
        om.changed.connect(self._om_changed)
        self._tick = QTimer(self, interval=5000, timeout=lambda: self.isVisible() and self.refresh_header())
        self._tick.start()
        self._run_changed(self.page.current)

    def __getattr__(self, name: str):  # the main window and tests talk to the page through us
        if name in ("page", "planner", "agent", "side", "split", "stack", "tasks", "body", "header") \
                or name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.page, name)

    # ------------------------------------------------------------------ header
    def refresh_header(self) -> None:
        pid = self.page.project.currentData()
        pr = self.db.project(pid) if isinstance(pid, int) else None
        if pr:
            self.projectBtn.set_project(pr.name, pr.path)
        elif pid == "auto":
            self.projectBtn.set_project("Auto", "picked from the goal")
        else:
            self.projectBtn.set_project("Add a project", "")
        o = self.db.orchestration(self.page.current) if self.page.current else None
        in_run = bool(o)
        for w in (self.backBtn, self.crumb, self.runBtn, self.runState, self.stopBtn, self.newGoal):
            w.setVisible(in_run)
        self.settingsBtn.setVisible(not in_run and pid is not None)
        lay = self.runState.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not o:
            return
        from orchestrator.ui.orchestrate_page import fmt_elapsed, model_line, run_pill

        goal = " ".join(o.goal.split())
        self.runBtn.setText((goal[:48] + "…" if len(goal) > 48 else goal) + "  ▾")
        self.runBtn.setToolTip(goal)
        state = self.page.run_state(o)
        lay.addWidget(run_pill(state, self.runState, fmt_elapsed((o.finished_at or time.time()) - o.created_at)
                               if state == "running" else ""), 0, Qt.AlignVCenter)
        self.stopBtn.setEnabled(self.om.is_active(o.id))
        tasks = self.db.tasks(o.id)
        self.sideTitle.setText(f"Tasks · {len(tasks)}" if tasks else "Tasks")
        self.sideInfo.setText(f"Planner {model_line(self.db, o.engine, o.model, o.effort)}  ·  "
                              f"up to {o.max_parallel} at once")

    def _project_menu(self) -> None:
        menu = RoundMenu(parent=self)
        cur = self.page.project.currentData()
        mine = [pr for pr in self.db.projects() if not pr.auto]
        for pr in mine:
            a = Action(FIF.ACCEPT if pr.id == cur else FIF.FOLDER, pr.name,
                       triggered=lambda _=False, i=pr.id: self.page.set_project(i))
            a.setToolTip(pr.path)
            menu.addAction(a)
        if mine:
            menu.addSeparator()
            menu.addAction(Action(FIF.ACCEPT if cur == "auto" else FIF.ROBOT, "Auto: pick from the goal",
                                  triggered=lambda: self.page.set_project("auto")))
        menu.addAction(Action(FIF.FOLDER_ADD, "Add project folder…", triggered=self.page.add_project))
        menu.exec(self.projectBtn.mapToGlobal(self.projectBtn.rect().bottomLeft()))

    def _run_menu(self) -> None:
        menu = RoundMenu(parent=self)
        pid = self.page.project.currentData()
        o = self.db.orchestration(self.page.current) if self.page.current else None
        runs = self.db.orchestrations(o.project_id if o and pid != "auto" else None)[:15]
        for r in runs:
            g = " ".join(r.goal.split())
            menu.addAction(Action(FIF.ACCEPT if o and r.id == o.id else FIF.ROBOT,
                                  f"{g[:50] + '…' if len(g) > 50 else g}  ·  {relative(r.created_at)}",
                                  triggered=lambda _=False, i=r.id: self.page.open_run(i)))
        menu.addSeparator()
        menu.addAction(Action(FIF.ADD, "New goal", triggered=self.new_goal))
        menu.exec(self.runBtn.mapToGlobal(self.runBtn.rect().bottomLeft()))

    def _om_changed(self, oid) -> None:
        if oid == self.page.current and self.tasks.isVisible():
            self.tasks.reload()
        if self.isVisible():
            self.refresh_header()

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
            self.body.setCurrentWidget(self.split)
            if not self._sized:
                self._sized = True
                w = max(2, self.split.width() or self.width())
                self.split.setSizes([w * 3 // 5, w - w * 3 // 5])
            if not self.planner.session or self.planner.session.id != sid:
                self.planner.show_session(sid)
                self.show_board()  # an open agent belongs to the previous run
        else:
            self.planner.show_session(None)
            self.body.setCurrentWidget(self.page)
        self.refresh_header()

    def show_tab(self, _key: str = "tasks") -> None:  # older callers: there is only the task list now
        self.show_board()

    def show_board(self) -> None:
        self.tasks.reload()
        self.stack.setCurrentWidget(self.tasks)
        if self.agent.session:
            self.agent.show_session(None)

    def new_goal(self) -> None:
        self.page.history.setCurrentIndex(0)  # the project's home; the run keeps going

    def open_task(self, tid: int) -> None:
        t = self.db.task(tid)
        if t and t.session_id:
            self.open_agent(t.session_id)

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
